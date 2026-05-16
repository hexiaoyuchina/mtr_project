from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class VtyshError(RuntimeError):
    pass


def _vtysh_bin() -> str:
    return os.environ.get("MTR_VTYSH_BIN", "vtysh").strip() or "vtysh"


def _run_vtysh(commands: List[str], timeout_s: int = 10) -> str:
    """
    Run vtysh in batch mode.

    Prefer a single vtysh invocation with stdin so that multi-line config is atomic-ish.
    """
    vtysh = _vtysh_bin()
    try:
        p = subprocess.run(
            [vtysh],
            input=("\n".join(commands) + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as e:
        raise VtyshError(f"vtysh_not_found: {vtysh}") from e
    except subprocess.TimeoutExpired as e:
        raise VtyshError("vtysh_timeout") from e
    out = p.stdout.decode("utf-8", errors="replace")
    if p.returncode != 0:
        raise VtyshError(f"vtysh_failed: rc={p.returncode}: {out.strip()[:500]}")
    return out


def _run_show(cmd: str, timeout_s: int = 8) -> str:
    vtysh = _vtysh_bin()
    try:
        p = subprocess.run(
            [vtysh, "-c", cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as e:
        raise VtyshError(f"vtysh_not_found: {vtysh}") from e
    except subprocess.TimeoutExpired as e:
        raise VtyshError("vtysh_timeout") from e
    out = p.stdout.decode("utf-8", errors="replace")
    if p.returncode != 0:
        raise VtyshError(f"vtysh_failed: rc={p.returncode}: {out.strip()[:500]}")
    return out


def _write_memory_best_effort() -> None:
    """持久化到 startup 配置；失败仅记日志（运行配置可能已生效）。"""
    if os.environ.get("MTR_FRR_WRITE_MEM", "1").strip().lower() in {"0", "false", "no"}:
        return
    vtysh = _vtysh_bin()
    try:
        p = subprocess.run(
            [vtysh, "-c", "write memory"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=12,
            check=False,
        )
    except OSError as e:
        logger.warning("frr write memory spawn failed: %s", e)
        return
    out = p.stdout.decode("utf-8", errors="replace")
    if p.returncode != 0:
        logger.warning("frr write memory failed rc=%s: %s", p.returncode, out.strip()[:400])
    else:
        logger.info("frr write memory ok")


def _apply_router_config(inst: "BgpInstance", inner_cmds: List[str], timeout_s: int = 18) -> None:
    body = ["configure terminal", _router_bgp_line(inst), *inner_cmds, "end"]
    _run_vtysh(body, timeout_s=timeout_s)
    _write_memory_best_effort()


@dataclass(frozen=True)
class BgpInstance:
    local_as: int
    vrf: str  # "default" or vrf name


@dataclass
class BgpNeighborSummary:
    vrf: str
    ip: str
    remote_as: int
    state: str
    pfx_rcd: int
    up_down: str
    enabled: bool
    neighbor_ver: int = 0
    msg_rcvd: int = 0
    msg_sent: int = 0
    tbl_ver: int = 0
    inq: int = 0
    outq: int = 0


@dataclass
class BgpRibEntry:
    """BGP IPv4 单播 RIB 中的一条路径（来自 FRR show / json）。"""

    prefix: str
    nexthop: str
    as_path: str = ""
    peer_hint: str = ""


_VRF_RE = re.compile(r"^router bgp\s+(?P<as>\d+)(?:\s+vrf\s+(?P<vrf>\S+))?\s*$", re.IGNORECASE)
_NEIGHBOR_SHUTDOWN_RE = re.compile(
    r"^neighbor\s+(\d{1,3}(?:\.\d{1,3}){3})\s+shutdown\s*$",
    re.IGNORECASE,
)
_NO_NEIGHBOR_SHUTDOWN_RE = re.compile(
    r"^no\s+neighbor\s+(\d{1,3}(?:\.\d{1,3}){3})\s+shutdown\s*$",
    re.IGNORECASE,
)


def neighbor_shutdown_by_vrf_from_running_config() -> Dict[str, set[str]]:
    """
    从 ``show running-config`` 解析各 VRF 下 ``neighbor <ip> shutdown`` 的邻居。

    ``show bgp … summary`` 在 admin shutdown 时 State 可能仍为 ``Idle``/``Active`` 等，
    不含 ``Admin``，仅用 summary 会误判 ``enabled=True``，导致 OP 开关刷新后弹回「开启」。
    """
    out = _run_show("show running-config", timeout_s=16)
    lines = out.splitlines()
    result: Dict[str, set[str]] = {}
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        m = _VRF_RE.match(line)
        if not m:
            i += 1
            continue
        vrf = (m.group("vrf") or "default").strip()
        ip_state: Dict[str, bool] = {}
        i += 1
        while i < n:
            sub = lines[i].strip()
            if sub == "!":
                i += 1
                break
            if _VRF_RE.match(sub):
                break
            m_down = _NEIGHBOR_SHUTDOWN_RE.match(sub)
            if m_down:
                ip_state[m_down.group(1)] = True
                i += 1
                continue
            m_up = _NO_NEIGHBOR_SHUTDOWN_RE.match(sub)
            if m_up:
                ip_state[m_up.group(1)] = False
                i += 1
                continue
            i += 1
        result[vrf] = {ip for ip, shut in ip_state.items() if shut}
    return result


def _router_bgp_line(inst: BgpInstance) -> str:
    if inst.vrf == "default":
        return f"router bgp {inst.local_as}"
    return f"router bgp {inst.local_as} vrf {inst.vrf}"


def list_bgp_instances() -> List[BgpInstance]:
    """
    Parse instances from running-config.

    Expected examples:
      router bgp 65201
      router bgp 65200 vrf vrf2102
      router bgp 65203 vrf vrf2103
    """
    out = _run_show("show running-config", timeout_s=12)
    seen: Dict[Tuple[str, int], bool] = {}
    items: List[BgpInstance] = []
    for line in out.splitlines():
        m = _VRF_RE.match(line.strip())
        if not m:
            continue
        local_as = int(m.group("as"))
        vrf = (m.group("vrf") or "default").strip()
        key = (vrf, local_as)
        if key in seen:
            continue
        seen[key] = True
        items.append(BgpInstance(local_as=local_as, vrf=vrf))
    # stable ordering: default first, then vrf name
    items.sort(key=lambda x: (0 if x.vrf == "default" else 1, x.vrf, x.local_as))
    return items


def get_instance_by_vrf(vrf: str) -> Optional[BgpInstance]:
    vrf = (vrf or "default").strip()
    for inst in list_bgp_instances():
        if inst.vrf == vrf:
            return inst
    return None


def list_kernel_vrf_names() -> List[str]:
    """本机 ``ip link type vrf`` 的接口名（如 vrf2102），用于 OP 列出尚未 ``router bgp`` 的 VRF。"""
    return sorted(kernel_vrf_devices().keys())


def kernel_vrf_devices() -> Dict[str, int]:
    """本机 ``ip link type vrf``：接口名 -> rt_table。"""
    out: Dict[str, int] = {}
    try:
        p = subprocess.run(
            ["ip", "-j", "link", "show", "type", "vrf"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if p.returncode != 0 or not (p.stdout or "").strip():
            return out
        data = json.loads(p.stdout)
        if not isinstance(data, list):
            return out
        for row in data:
            if not isinstance(row, dict):
                continue
            ifname = (row.get("ifname") or "").strip()
            if not ifname:
                continue
            info = row.get("linkinfo") if isinstance(row.get("linkinfo"), dict) else {}
            kind = (info.get("info_kind") or row.get("link_type") or "").strip().lower()
            if kind != "vrf" and row.get("link_type") != "vrf":
                continue
            info_data = info.get("info_data") if isinstance(info.get("info_data"), dict) else {}
            try:
                tid = int(info_data.get("table") or 0)
            except (TypeError, ValueError):
                tid = 0
            if tid > 0:
                out[ifname] = tid
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError, ValueError):
        return out
    return out


_KERNEL_VRF_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,14}$")


def validate_kernel_vrf_ifname(vrf: str) -> str:
    """Linux 接口名约束（VRF 设备）：1–15 字符，字母数字与 ``._-``。"""
    vrf_n = (vrf or "").strip()
    if vrf_n == "default":
        raise VtyshError("kernel_vrf_reserved_default")
    if not _KERNEL_VRF_NAME_RE.match(vrf_n):
        raise VtyshError(
            "kernel_vrf_invalid_name: VRF 须为合法接口名（1–15 字符，字母数字与 . _ -，不以符号开头）"
        )
    return vrf_n


def allocate_rt_table_for_kernel_vrf() -> int:
    used = set(kernel_vrf_devices().values())
    lo = int(os.environ.get("MTR_BGP_AUTO_VRF_TABLE_MIN") or "30200")
    hi = int(os.environ.get("MTR_BGP_AUTO_VRF_TABLE_MAX") or "64999")
    for t in range(lo, hi + 1):
        if t not in used:
            return t
    raise VtyshError("no_free_rt_table_for_kernel_vrf")


def ensure_kernel_vrf(vrf: str, rt_table: Optional[int] = None) -> Dict[str, Any]:
    """
    若内核尚无 ``ip link type vrf`` 设备则 ``ip link add <name> type vrf table <id>`` 并 ``up``。

    已存在则返回 ``created: False``。需 CAP_NET_ADMIN（通常 root 跑 OP）。
    """
    vrf_n = validate_kernel_vrf_ifname(vrf)
    existing = kernel_vrf_devices()
    if vrf_n in existing:
        return {"vrf": vrf_n, "rt_table": int(existing[vrf_n]), "created": False}
    tid = int(rt_table) if rt_table is not None else allocate_rt_table_for_kernel_vrf()
    used = set(existing.values())
    if tid in used:
        tid = allocate_rt_table_for_kernel_vrf()
    p = subprocess.run(
        ["ip", "link", "add", vrf_n, "type", "vrf", "table", str(tid)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    err = ((p.stderr or "") + (p.stdout or "")).strip()
    if p.returncode != 0:
        if "File exists" in err or "exists" in err.lower():
            return {"vrf": vrf_n, "rt_table": tid, "created": False, "note": "race_or_exists"}
        raise VtyshError(f"kernel_vrf_create_failed: {err[:400]}")
    subprocess.run(["ip", "link", "set", vrf_n, "up"], capture_output=True, text=True, timeout=8, check=False)
    return {"vrf": vrf_n, "rt_table": tid, "created": True}


def default_local_as_for_new_instance() -> int:
    """新建 ``router bgp … vrf …`` 时默认 AS：环境变量 > 已有非 default 实例 > 任意实例 > 65200。"""
    raw = (os.environ.get("MTR_BGP_ENSURE_LOCAL_AS") or "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        insts = list_bgp_instances()
    except VtyshError:
        return 65200
    for inst in insts:
        if inst.vrf != "default":
            return int(inst.local_as)
    for inst in insts:
        return int(inst.local_as)
    return 65200


def ensure_bgp_instance(
    vrf: str,
    local_as: int,
    *,
    router_id: Optional[str] = None,
) -> BgpInstance:
    """
    若 FRR 尚无 ``router bgp <as> vrf <vrf>`` 则创建空实例（便于随后 ``neighbor``）。

    若该 VRF 已存在 BGP 实例且 AS 与 ``local_as`` 不一致则报错。
    ``router-id`` 可选（FRR 部分版本对 VRF BGP 建议显式指定）；可由调用方或 ``MTR_BGP_ENSURE_ROUTER_ID`` 提供。
    """
    vrf_n = (vrf or "default").strip()
    if vrf_n == "default":
        raise VtyshError("ensure_bgp_instance: default vrf must exist; configure router bgp <as> manually")
    existing = get_instance_by_vrf(vrf_n)
    if existing:
        if int(existing.local_as) != int(local_as):
            raise VtyshError(
                f"bgp_vrf_as_mismatch vrf={vrf_n} existing_as={existing.local_as} requested_as={local_as}"
            )
        return existing
    rid = ((router_id or "").strip() or (os.environ.get("MTR_BGP_ENSURE_ROUTER_ID") or "").strip()) or None
    inner_lines: List[str] = [f"router bgp {int(local_as)} vrf {vrf_n}"]
    if rid:
        inner_lines.append(f" bgp router-id {rid}")
    if os.environ.get("MTR_BGP_DISABLE_EBGP_REQUIRES_POLICY", "1").strip().lower() not in {"0", "false", "no"}:
        inner_lines.append(" no bgp ebgp-requires-policy")
    logger.info(f"Creating BGP instance: vrf={vrf_n} local_as={local_as} router_id={rid}")
    logger.info(f"vtysh commands: {inner_lines}")
    try:
        output = _run_vtysh(["configure terminal", *inner_lines, "end"], timeout_s=24)
        logger.info(f"vtysh configure output: {output.strip()[:200]}")
    except VtyshError as e:
        logger.error(f"Failed to configure BGP instance: {e}")
        raise
    _write_memory_best_effort()
    got = get_instance_by_vrf(vrf_n)
    if not got:
        logger.error(f"ensure_bgp_instance_failed: vrf={vrf_n} not found after configuration")
        try:
            current_instances = list_bgp_instances()
            logger.error(f"Current BGP instances: {[f'{i.vrf} ({i.local_as})' for i in current_instances]}")
        except Exception as e2:
            logger.error(f"Failed to list BGP instances: {e2}")
        raise VtyshError(f"ensure_bgp_instance_failed vrf={vrf_n}")
    logger.info(f"Created BGP instance successfully: vrf={vrf_n} local_as={got.local_as}")
    return got


def _show_bgp_summary_text(vrf: str) -> str:
    if vrf and vrf != "default":
        last_err: Optional[Exception] = None
        for cmd in (f"show bgp vrf {vrf} ipv4 unicast summary", f"show bgp vrf {vrf} summary"):
            try:
                return _run_show(cmd, timeout_s=12)
            except VtyshError as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        return ""
    last_err_d: Optional[Exception] = None
    for cmd in ("show bgp ipv4 unicast summary", "show bgp summary"):
        try:
            return _run_show(cmd, timeout_s=12)
        except VtyshError as e:
            last_err_d = e
            continue
    if last_err_d:
        raise last_err_d
    return ""


def _parse_summary_text(vrf: str, out: str) -> List[BgpNeighborSummary]:
    """
    Parse FRR ``show bgp ... summary`` neighbor lines.

    标准列：Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd
    兼容旧格式（列较少时仍解析 AS 与末尾 Up/Down、State/PfxRcd）。
    """
    rows: List[BgpNeighborSummary] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("neighbor") or line.startswith("BGP") or line.startswith("Total"):
            continue
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}\b", line):
            continue
        parts = re.split(r"\s+", line)
        if len(parts) < 6:
            continue
        ip = parts[0]
        neighbor_ver = msg_rcvd = msg_sent = tbl_ver = inq = outq = 0
        up_down = ""
        state_pfx = ""
        remote_as = 0
        try:
            if len(parts) >= 10 and parts[1].isdigit():
                neighbor_ver = int(parts[1])
                remote_as = int(parts[2])
                msg_rcvd = int(parts[3])
                msg_sent = int(parts[4])
                tbl_ver = int(parts[5])
                inq = int(parts[6])
                outq = int(parts[7])
                up_down = parts[8]
                state_pfx = parts[9]
            else:
                remote_as = int(parts[2])
                up_down = parts[-2] if len(parts) >= 2 else ""
                state_pfx = parts[-1]
        except (ValueError, IndexError):
            continue
        state = state_pfx
        pfx_rcd = 0
        if re.fullmatch(r"\d+", state_pfx):
            state = "Established"
            pfx_rcd = int(state_pfx)
        enabled = "admin" not in state.lower()
        rows.append(
            BgpNeighborSummary(
                vrf=vrf or "default",
                ip=ip,
                remote_as=remote_as,
                state=state,
                pfx_rcd=pfx_rcd,
                up_down=up_down,
                enabled=enabled,
                neighbor_ver=neighbor_ver,
                msg_rcvd=msg_rcvd,
                msg_sent=msg_sent,
                tbl_ver=tbl_ver,
                inq=inq,
                outq=outq,
            )
        )
    return rows


def list_bgp_neighbors(
    vrf: str,
    shutdown_by_vrf: Optional[Dict[str, set[str]]] = None,
) -> List[BgpNeighborSummary]:
    """
    :param shutdown_by_vrf: 若已批量拉取 ``neighbor_shutdown_by_vrf_from_running_config()`` 可传入，
        避免同一请求内重复 ``show running-config``；为 ``None`` 时本函数内会拉取一次。
    """
    vrf_norm = (vrf or "default").strip()
    out = _show_bgp_summary_text(vrf_norm)
    rows = _parse_summary_text(vrf_norm, out)
    sd = shutdown_by_vrf
    if sd is None:
        try:
            sd = neighbor_shutdown_by_vrf_from_running_config()
        except VtyshError:
            sd = {}
    shut = sd.get(vrf_norm, set())
    for r in rows:
        if r.ip in shut:
            r.enabled = False
    return rows


def _parse_bgp_rib_json(text: str) -> List[BgpRibEntry]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    routes_obj = data.get("routes")
    if not isinstance(routes_obj, dict):
        uni = data.get("ipv4Unicast")
        if isinstance(uni, dict):
            routes_obj = uni.get("routes") or uni.get("rib") or {}
    if not isinstance(routes_obj, dict):
        return []
    out: List[BgpRibEntry] = []
    for prefix_key, entries in routes_obj.items():
        if entries is None:
            continue
        entries_l = entries if isinstance(entries, list) else [entries]
        for ent in entries_l:
            if not isinstance(ent, dict):
                continue
            if ent.get("valid") is False:
                continue
            nh = ""
            nxs = ent.get("nexthops")
            if isinstance(nxs, list) and nxs:
                h0 = nxs[0]
                if isinstance(h0, dict):
                    nh = str(h0.get("ip") or h0.get("hostname") or "").strip()
            if not nh:
                nh = str(ent.get("nexthop") or ent.get("nextHop") or "").strip()
            path_o = ent.get("aspath")
            if isinstance(path_o, dict):
                path_s = str(path_o.get("string") or "").strip()
            else:
                path_s = str(ent.get("path") or path_o or "").strip()
            peer = str(ent.get("peerId") or "")
            if not peer and isinstance(ent.get("peer"), dict):
                peer = str(ent.get("peer", {}).get("peerId") or "")
            prefix = str(prefix_key)
            if "/" not in prefix:
                pfx = ent.get("prefix")
                plen = ent.get("prefixLen")
                if pfx and plen is not None:
                    try:
                        prefix = f"{pfx}/{int(plen)}"
                    except (TypeError, ValueError):
                        pass
            out.append(BgpRibEntry(prefix=prefix, nexthop=nh, as_path=path_s, peer_hint=peer))
    return out


_RIB_LINE_SEARCH = re.compile(r"(?P<prefix>\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\s+(?P<nh>\d{1,3}(?:\.\d{1,3}){3})\b")


def _parse_bgp_rib_text(text: str) -> List[BgpRibEntry]:
    out: List[BgpRibEntry] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("BGP") or line.startswith("Status") or line.startswith("Origin"):
            continue
        if "Network" in line and "Next Hop" in line:
            continue
        if line.startswith("Displayed") or line.startswith("Some paths") or line.startswith("For address"):
            continue
        if line.strip().startswith("Route Distinguisher"):
            continue
        m = _RIB_LINE_SEARCH.search(line)
        if not m:
            continue
        prefix = m.group("prefix")
        nh = m.group("nh")
        rest = line[m.end() :].strip()
        out.append(BgpRibEntry(prefix=prefix, nexthop=nh, as_path=rest, peer_hint=""))
    return out


def list_bgp_ipv4_unicast_rib(vrf: str) -> List[BgpRibEntry]:
    """
    读取 FRR BGP IPv4 单播 RIB。依次尝试 ``… json`` 与文本 ``show bgp …``。
    """
    vrf_n = (vrf or "default").strip()
    if vrf_n != "default":
        cmds = [
            f"show bgp vrf {vrf_n} ipv4 unicast json",
            f"show bgp vrf {vrf_n} ipv4 unicast",
            f"show bgp vrf {vrf_n} ipv4 unicast wide",
        ]
    else:
        cmds = [
            "show bgp ipv4 unicast json",
            "show bgp ipv4 unicast",
            "show bgp ipv4 unicast wide",
        ]
    last_err: Optional[Exception] = None
    for cmd in cmds:
        try:
            raw = _run_show(cmd, timeout_s=90)
        except VtyshError as e:
            last_err = e
            continue
        t = raw.strip()
        if cmd.endswith("json") and t.startswith("{"):
            rows = _parse_bgp_rib_json(t)
            if rows:
                return rows
            continue
        rows = _parse_bgp_rib_text(raw)
        if rows:
            return rows
    if last_err:
        raise last_err
    return []


def _add_neighbor_route(vrf: str, neighbor_ip: str, source_ip: str, egress_iface: str) -> None:
    """为BGP邻居添加直连路由。"""
    import subprocess
    try:
        # 先检查路由是否已存在
        result = subprocess.run(
            ["ip", "route", "show", "vrf", vrf, neighbor_ip],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and neighbor_ip in result.stdout:
            logger.info("route already exists for vrf=%s neighbor=%s", vrf, neighbor_ip)
            return
        
        # 添加路由 - 使用egress_iface
        cmd = ["ip", "route", "add", "vrf", vrf, f"{neighbor_ip}/32", "src", source_ip]
        if egress_iface:
            cmd.extend(["dev", egress_iface])
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            logger.warning("failed to add route for vrf=%s neighbor=%s: %s", 
                        vrf, neighbor_ip, result.stderr[:200])
        else:
            logger.info("added route for vrf=%s neighbor=%s via %s", vrf, neighbor_ip, egress_iface)
    except Exception as e:
        logger.warning("error adding route for vrf=%s neighbor=%s: %s", vrf, neighbor_ip, str(e))


def set_neighbor_enabled(vrf: str, neighbor_ip: str, enabled: bool) -> None:
    inst = get_instance_by_vrf(vrf)
    if not inst:
        raise VtyshError(f"bgp_instance_not_found: {vrf}")
    if enabled:
        inner = [f"no neighbor {neighbor_ip} shutdown"]
        # 获取update-source和egress_iface并添加路由
        try:
            import sqlite3
            from pathlib import Path
            db_path = Path(os.environ.get("MTR_OP_DB", "/root/mtr_op/data.db"))
            if db_path.is_file():
                conn = sqlite3.connect(str(db_path))
                try:
                    # 从bgp_neighbor_meta获取source_ip
                    row = conn.execute(
                        "SELECT source_ip FROM bgp_neighbor_meta WHERE vrf = ? AND neighbor_ip = ?",
                        (vrf, neighbor_ip)
                    ).fetchone()
                    source_ip = row[0] if row else ""
                    
                    # 从arp_spoof_targets获取egress_iface
                    row = conn.execute(
                        "SELECT egress_iface FROM arp_spoof_targets WHERE satellite_vrf = ? AND enabled = 1",
                        (vrf,)
                    ).fetchone()
                    egress_iface = row[0] if row else ""
                    
                    if source_ip:
                        _add_neighbor_route(vrf, neighbor_ip, source_ip, egress_iface)
                finally:
                    conn.close()
        except Exception as e:
            logger.warning("error getting route info: %s", str(e))
    else:
        inner = [f"neighbor {neighbor_ip} shutdown"]
    _apply_router_config(inst, inner, timeout_s=14)


def set_neighbor_ebgp_multihop(vrf: str, neighbor_ip: str, hops: Optional[int]) -> None:
    """``neighbor … ebgp-multihop`` 或 ``no neighbor … ebgp-multihop``（hops 空/≤0 则删除）。"""
    inst = get_instance_by_vrf(vrf)
    if not inst:
        raise VtyshError(f"bgp_instance_not_found: {vrf}")
    nip = (neighbor_ip or "").strip()
    if not nip:
        raise VtyshError("invalid_neighbor_ip")
    if hops is not None and int(hops) > 0:
        inner = [f"neighbor {nip} ebgp-multihop {int(hops)}"]
    else:
        inner = [f"no neighbor {nip} ebgp-multihop"]
    _apply_router_config(inst, inner, timeout_s=14)


def set_neighbor_update_source(vrf: str, neighbor_ip: str, source_ip: Optional[str]) -> None:
    """``neighbor … update-source …`` 或 ``no neighbor … update-source``（空则删除）。"""
    inst = get_instance_by_vrf(vrf)
    if not inst:
        raise VtyshError(f"bgp_instance_not_found: {vrf}")
    nip = (neighbor_ip or "").strip()
    if not nip:
        raise VtyshError("invalid_neighbor_ip")
    src = (source_ip or "").strip()
    if src:
        inner = [f"neighbor {nip} update-source {src}"]
    else:
        inner = [f"no neighbor {nip} update-source"]
    _apply_router_config(inst, inner, timeout_s=14)


def ebgp_multihop_satellite_default() -> Optional[int]:
    """
    卫星 VRF 经 veth 连对端时 FRR 常需 ebgp-multihop；由环境变量控制。

    ``MTR_SATELLITE_BGP_EBGP_MULTIHOP``：默认 ``5``；``0``/空/``off`` 表示不下发 multihop。
    """
    raw = (os.environ.get("MTR_SATELLITE_BGP_EBGP_MULTIHOP") or "5").strip().lower()
    if raw in {"", "0", "off", "no", "false"}:
        return None
    try:
        n = int(raw)
        return n if n > 0 else None
    except ValueError:
        return 5


def add_neighbor_ipv4_unicast(
    vrf: str,
    neighbor_ip: str,
    remote_as: int,
    update_source: Optional[str] = None,
    ebgp_multihop: Optional[int] = None,
) -> None:
    inst = get_instance_by_vrf(vrf)
    if not inst:
        raise VtyshError(f"bgp_instance_not_found: {vrf}")
    inner: List[str] = [
        f"neighbor {neighbor_ip} remote-as {int(remote_as)}",
    ]
    us = (update_source or "").strip()
    if us:
        inner.append(f"neighbor {neighbor_ip} update-source {us}")
    mh = int(ebgp_multihop) if ebgp_multihop is not None else None
    if mh is not None and mh > 0:
        inner.append(f"neighbor {neighbor_ip} ebgp-multihop {mh}")
    inner.extend(
        [
        "address-family ipv4 unicast",
        f"neighbor {neighbor_ip} activate",
        ]
    )
    if os.environ.get("MTR_BGP_ADD_NHS", "1").strip().lower() not in {"0", "false", "no"}:
        inner.append(f"neighbor {neighbor_ip} next-hop-self")
    inner.append("exit-address-family")
    _apply_router_config(inst, inner, timeout_s=20)


def remove_neighbor_ipv4(vrf: str, neighbor_ip: str) -> None:
    inst = get_instance_by_vrf(vrf)
    if not inst:
        raise VtyshError(f"bgp_instance_not_found: {vrf}")
    inner: List[str] = [
        "address-family ipv4 unicast",
        f"no neighbor {neighbor_ip} activate",
        "exit-address-family",
        f"no neighbor {neighbor_ip}",
    ]
    _apply_router_config(inst, inner, timeout_s=22)


def remove_router_bgp_vrf(vrf: str) -> None:
    inst = get_instance_by_vrf(vrf)
    if not inst:
        raise VtyshError(f"bgp_instance_not_found: {vrf}")
    _vtysh_config([f"no router bgp {inst.asn} vrf {vrf}"])


def replace_neighbor_remote_as(
    vrf: str,
    neighbor_ip: str,
    new_remote_as: int,
    restore_shutdown: bool,
    update_source: Optional[str] = None,
    ebgp_multihop: Optional[int] = None,
) -> None:
    """FRR 无可靠 in-place 改 remote-as：先删再加，可选恢复 shutdown 管理态。"""
    remove_neighbor_ipv4(vrf, neighbor_ip)
    add_neighbor_ipv4_unicast(
        vrf, neighbor_ip, int(new_remote_as), update_source=update_source, ebgp_multihop=ebgp_multihop
    )
    if restore_shutdown:
        set_neighbor_enabled(vrf, neighbor_ip, False)


def rename_neighbor_ipv4(
    vrf: str,
    old_ip: str,
    new_ip: str,
    remote_as: int,
    update_source: Optional[str],
    enabled: bool,
    ebgp_multihop: Optional[int] = None,
) -> None:
    """将邻居从 ``old_ip`` 改为 ``new_ip``（FRR 无 in-place 改地址：删旧建新）。"""
    o = (old_ip or "").strip()
    n = (new_ip or "").strip()
    if not o or not n or o == n:
        raise VtyshError("invalid_rename_neighbor")
    remove_neighbor_ipv4(vrf, o)
    add_neighbor_ipv4_unicast(vrf, n, int(remote_as), update_source=update_source, ebgp_multihop=ebgp_multihop)
    if not enabled:
        set_neighbor_enabled(vrf, n, False)


def neighbor_is_established(vrf: str, neighbor_ip: str, shutdown_by_vrf: Optional[Dict[str, set[str]]] = None) -> bool:
    """给定邻居 IP 是否处于 Established（且未被 admin shutdown）。"""
    ip = (neighbor_ip or "").strip()
    if not ip:
        return False
    vrf_n = (vrf or "default").strip()
    try:
        rows = list_bgp_neighbors(vrf_n, shutdown_by_vrf)
    except VtyshError:
        return False
    for n in rows:
        if n.ip == ip:
            if not n.enabled:
                return False
            st = (n.state or "").strip().lower()
            # 新版 FRR 多为 ``Established``；兼容含该子串的变体
            return "established" in st
    return False


def set_bgp_ipv4_network(vrf: str, prefix: str, enable: bool) -> None:
    """
    在 ``router bgp … vrf …`` 的 IPv4 单播族中增加或删除 ``network <prefix>``。
    需内核/VRF 中已有匹配前缀的路由（通常配合 blackhole static）。
    """
    inst = get_instance_by_vrf(vrf)
    if not inst:
        raise VtyshError(f"bgp_instance_not_found: {vrf}")
    pfx = (prefix or "").strip()
    if not pfx or "/" not in pfx:
        raise VtyshError(f"invalid_prefix: {prefix}")
    if enable:
        inner = ["address-family ipv4 unicast", f"network {pfx}", "exit-address-family"]
    else:
        inner = ["address-family ipv4 unicast", f"no network {pfx}", "exit-address-family"]
    _apply_router_config(inst, inner, timeout_s=22)


def add_bgp_networks_batch(vrf: str, prefixes_with_nexthop: list, timeout_s: int = 60) -> dict:
    """
    批量向 BGP 添加网络宣告（用于将从数据库中学到的路由通告给邻居）。
    高性能优化版本：
    - 路由预处理（去重、排序）
    - 智能速率控制（根据系统负载动态调整）
    - 多线程并行执行
    - 系统参数优化
    
    prefixes_with_nexthop: [(prefix, nexthop), ...] 列表
    返回: {"added": 数量, "failed": 数量, "errors": [...]}
    """
    from .frr_bgp_optimized import add_bgp_networks_optimized
    
    if not prefixes_with_nexthop:
        return {"added": 0, "failed": 0, "errors": [], "method": "optimized-parallel"}
    
    result = add_bgp_networks_optimized(vrf, prefixes_with_nexthop)
    
    return {
        "added": result["added"],
        "failed": result["failed"],
        "errors": [],
        "method": result["method"],
        "elapsed": result["elapsed"],
        "speed": result["speed"]
    }


def _ensure_redistribute_static(vrf: str) -> None:
    """
    确保BGP配置中启用了redistribute static
    """
    try:
        inst = get_instance_by_vrf(vrf)
        if inst:
            inner = [
                "address-family ipv4 unicast",
                "redistribute static",
                "exit-address-family"
            ]
            _apply_router_config(inst, inner, timeout_s=10)
    except Exception as e:
        logger.warning(f"Failed to ensure redistribute static: {e}")


def _add_bgp_networks_fast(vrf: str, inst, prefixes_with_nexthop: list, timeout_s: int) -> int:
    """
    快速添加 BGP network 宣告
    使用 vtysh batch 模式
    """
    try:
        # 生成分批配置
        batch_size = 5000
        for i in range(0, len(prefixes_with_nexthop), batch_size):
            batch = prefixes_with_nexthop[i:i + batch_size]
            inner = ["address-family ipv4 unicast"]
            for prefix, _ in batch:
                inner.append(f"network {prefix}")
            inner.append("exit-address-family")
            _apply_router_config(inst, inner, timeout_s=timeout_s)
        return len(prefixes_with_nexthop)
    except Exception:
        return 0


def _add_bgp_networks_large_batch(vrf: str, prefixes_with_nexthop: list, timeout_s: int) -> dict:
    """
    处理大规模路由（>1000条）的高效批量添加函数。
    使用临时文件批量配置，减少 vtysh 调用次数。
    """
    import subprocess
    import tempfile
    import os

    inst = get_instance_by_vrf(vrf)
    if not inst:
        return {"added": 0, "failed": len(prefixes_with_nexthop), "errors": ["bgp_instance_not_found"]}

    total = len(prefixes_with_nexthop)
    added = 0
    errors = []

    try:
        route_commands = []
        network_commands = []

        for prefix, nexthop in prefixes_with_nexthop:
            if nexthop:
                route_commands.append(f"ip route add vrf {vrf} {prefix} via {nexthop}")
            network_commands.append(f"network {prefix}")

        if route_commands:
            route_script = "\n".join(route_commands) + "\n"
            with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
                f.write(route_script)
                route_file = f.name

            try:
                subprocess.run(["bash", route_file], capture_output=True, text=True, timeout=timeout_s * 2)
            finally:
                os.unlink(route_file)

        if network_commands:
            config_lines = [
                f"router bgp {inst.local_as} vrf {vrf}",
                "address-family ipv4 unicast"
            ] + network_commands + [
                "exit-address-family",
                "exit"
            ]

            with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
                f.write("\n".join(config_lines) + "\n")
                config_file = f.name

            try:
                result = subprocess.run(
                    ["vtysh", "-f", config_file],
                    capture_output=True,
                    text=True,
                    timeout=timeout_s * 2
                )
                if result.returncode == 0:
                    subprocess.run(["vtysh", "-c", "write memory"], capture_output=True, text=True)
                    added = total
                else:
                    errors.append(result.stderr[:200])
            finally:
                os.unlink(config_file)

    except Exception as e:
        errors.append(str(e)[:200])
        logger.error("large batch add failed: %s", str(e)[:200])

    return {"added": added, "failed": total - added, "errors": errors}


def remove_bgp_networks_batch(vrf: str, prefixes: list, timeout_s: int = 60) -> dict:
    """
    批量从 BGP 删除网络宣告。
    prefixes: [prefix1, prefix2, ...] 列表
    返回: {"removed": 数量, "failed": 数量, "errors": [...]}
    """
    inst = get_instance_by_vrf(vrf)
    if not inst:
        raise VtyshError(f"bgp_instance_not_found: {vrf}")

    if not prefixes:
        return {"removed": 0, "failed": 0, "errors": []}

    batch_size = 500
    removed = 0
    failed = 0
    errors = []

    for i in range(0, len(prefixes), batch_size):
        batch = prefixes[i:i + batch_size]

        inner = ["address-family ipv4 unicast"]
        for prefix in batch:
            inner.append(f"no network {prefix}")
        inner.append("exit-address-family")

        try:
            _apply_router_config(inst, inner, timeout_s=timeout_s)
            removed += len(batch)
        except VtyshError as e:
            failed += len(batch)
            errors.append(str(e)[:100])
            logger.warning("batch remove networks failed: %s", str(e)[:200])

    return {"removed": removed, "failed": failed, "errors": errors}
