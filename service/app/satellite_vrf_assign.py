"""
ARP 引流条目触发时，自动创建「卫星 VRF」供多会话 BGP（做法 B）。

在 OP 保存 ``arp_spoof_targets`` 后由 ``main.py`` 调用（与 ``arp_spoof_assign`` 类似）。
需 **root**、本机 ``ip``；默认 **关闭**，打开见环境变量说明。

环境变量：

- ``MTR_AUTO_SATELLITE_VRF``：``0``/空/``off`` 关闭；``cidr`` 仅匹配 ``MTR_AUTO_SATELLITE_VRF_MATCH``；
  ``all`` 任意已启用 IPv4 冒充地址；``note`` 仅当条目的 ``note`` 含 ``MTR_AUTO_SATELLITE_VRF_NOTE`` 子串时创建。
- ``MTR_AUTO_SATELLITE_VRF_MATCH``：默认 ``10.133.152.0/24``（仅 ``cidr`` 模式）。
- ``MTR_AUTO_SATELLITE_VRF_NOTE``：默认 ``AUTOSAT``（仅 ``note`` 模式）。
- ``MTR_SATELLITE_PHY_VRF``：卫星 veth 对端挂到的现网 VRF；默认 ``vrf2102``。设为 ``default`` / ``main`` / 空
  时表示 **主表（default 命名空间）**，对 veth 执行 ``nomaster``，冒充 /32 写入 **主路由表**（不再硬编码 table 2102）。
  当 **主表 + ``MTR_SATELLITE_BGP_TCP_SOURCE=spoof``** 且 Linux 201 仍以 ``10.133.152.25x`` 为 BGP 邻居时，须在 200 上配置 **nft inet nat_sat_bgp**（BGP TCP/179 SNAT），见仓库 ``scripts/ensure_nat_sat_bgp_linux200.sh``；否则请改用 **underlay** 且 201 neighbor 改为对应 veth 本端 ``10.255.x.1``。
- ``MTR_SATELLITE_PEER_IP``：卫星 VRF 内 ``ip route … host`` 指向的对端（BGP 邻居地址），默认 ``10.133.152.204``。
- ``MTR_SATELLITE_VRF_PREFIX``：VRF 名前缀，默认 ``vbgp``，完整名为 ``{prefix}{末字节}``（如 ``vbgp250``）。
- ``MTR_AUTO_SATELLITE_VRF_ASSIGN=0``：仅打印计划不下发 ``ip``（调试用）。
- ``MTR_SATELLITE_BGP_TCP_SOURCE``：``underlay``（默认）或 ``spoof``。与 OP BGP 配合：默认用 veth 本端
  ``10.255.x.1`` 作为 ``neighbor … update-source``，避免上联丢弃「仅 dummy 上存在的冒充源」的 TCP；
  ``spoof`` 则不在 OP 侧自动填源，由你显式填冒充网关 IP（旧行为）。
- **Linux 201**：在 ``underlay`` 模式下，对端 FRR 的 ``neighbor`` 须指向 **200 上卫星 veth 在卫星 VRF 内的本端地址**
  （``10.255.x.1``，与 ``update-source`` 一致）；**不是** ``10.255.x.2``（对端在现网 VRF 内，仅作转发下一跳）。

状态文件（分配 rt_table 与 veth 网段，避免冲突）：``<MTR_OP_DB 父目录>/.satellite_vrf_assign.json``。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os

try:
    from . import frr_bgp
except ImportError:
    frr_bgp = None
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_STATE_NAME = ".satellite_vrf_assign.json"


def _mode() -> str:
    m = (os.environ.get("MTR_AUTO_SATELLITE_VRF") or "0").strip().lower()
    if m in {"1", "yes", "true", "on"}:
        return "cidr"
    return m


def _assign_disabled() -> bool:
    m = _mode()
    if m in {"", "0", "off", "false", "no", "none"}:
        return True
    if m not in {"all", "cidr", "note"}:
        return True
    return False


def _match_cidr() -> str:
    return (os.environ.get("MTR_AUTO_SATELLITE_VRF_MATCH") or "10.133.152.0/24").strip()


def _note_tag() -> str:
    return (os.environ.get("MTR_AUTO_SATELLITE_VRF_NOTE") or "AUTOSAT").strip()


def _phy_vrf() -> str:
    return (os.environ.get("MTR_SATELLITE_PHY_VRF") or "vrf2102").strip()


def _phy_is_main(phy: str) -> bool:
    """主表承载 152 物理口时，phy 配置为 default / main / 0 / 空。"""
    p = (phy or "").strip().lower()
    return p in {"", "default", "main", "0"}


def _peer_ip() -> str:
    return (os.environ.get("MTR_SATELLITE_PEER_IP") or "10.133.152.204").strip()


def _vrf_prefix() -> str:
    p = (os.environ.get("MTR_SATELLITE_VRF_PREFIX") or "vbgp").strip()
    return p if p else "vbgp"


def _assign_ip_cmds() -> bool:
    return (os.environ.get("MTR_AUTO_SATELLITE_VRF_ASSIGN", "1").strip().lower() not in {"0", "false", "no"})


def _run(cmd: List[str], timeout: int = 30) -> Tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def state_path(db_path: Path) -> Path:
    return db_path.parent / _STATE_NAME


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {"by_spoof_ip": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"by_spoof_ip": {}}
        if "by_spoof_ip" not in raw or not isinstance(raw["by_spoof_ip"], dict):
            raw["by_spoof_ip"] = {}
        return raw
    except (OSError, json.JSONDecodeError, TypeError):
        return {"by_spoof_ip": {}}


def _save_state(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data, indent=0, sort_keys=True), encoding="utf-8")
    except OSError as e:
        logger.warning("satellite_vrf_assign: save state %s: %s", path, e)


def _list_kernel_vrf_tables() -> Dict[str, int]:
    """ifname -> rt_table"""
    code, out = _run(["ip", "-j", "link", "show", "type", "vrf"])
    if code != 0 or not (out or "").strip():
        return {}
    out_map: Dict[str, int] = {}
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return {}
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = (row.get("ifname") or "").strip()
        li = row.get("linkinfo")
        if not name or not isinstance(li, dict):
            continue
        if (li.get("info_kind") or "").lower() != "vrf":
            continue
        info_data = li.get("info_data") or {}
        if not isinstance(info_data, dict):
            continue
        try:
            tid = int(info_data.get("table") or 0)
        except (TypeError, ValueError):
            continue
        if tid > 0:
            out_map[name] = tid
    return out_map


def _iface_exists(name: str) -> bool:
    code, _ = _run(["ip", "link", "show", "dev", name])
    return code == 0


def _phy_iface_ok(phy: str) -> bool:
    """主表模式不检查名为 default 的 link。"""
    if _phy_is_main(phy):
        return True
    return _iface_exists(phy)


def _phy_route_replace_spoof(spoof_ip: str, via: str, dev: str, phy: str) -> Tuple[int, str]:
    if _phy_is_main(phy):
        return _run(["ip", "route", "replace", f"{spoof_ip}/32", "via", via, "dev", dev])
    return _run(["ip", "route", "replace", f"{spoof_ip}/32", "via", via, "dev", dev, "vrf", phy])


def _phy_route_del_spoof(spoof_ip: str, via: str, dev: str, phy: str) -> Tuple[int, str]:
    if _phy_is_main(phy):
        for cmd in (
            ["ip", "route", "del", f"{spoof_ip}/32", "via", via, "dev", dev],
            ["ip", "route", "del", f"{spoof_ip}/32", "dev", dev],
        ):
            rc, out = _run(cmd)
            if rc == 0:
                return rc, out
        return _run(["ip", "route", "del", f"{spoof_ip}/32"])
    rc, out = _run(["ip", "route", "del", f"{spoof_ip}/32", "vrf", phy])
    if rc != 0:
        rc, out = _run(["ip", "route", "del", f"{spoof_ip}/32", "via", via, "dev", dev, "vrf", phy])
    return rc, out


def _pick_table(last_octet: int, used: Set[int]) -> int:
    cand = 30200 + int(last_octet)
    if cand not in used:
        return cand
    for t in range(30250, 65000):
        if t not in used:
            return t
    return 65001


def _next_veth_third(state: Dict[str, Any], used_thirds: Set[int]) -> int:
    by = state.get("by_spoof_ip") or {}
    mx = 210
    if isinstance(by, dict):
        for _k, v in by.items():
            if isinstance(v, dict):
                try:
                    th = int(v.get("veth_third") or 0)
                except (TypeError, ValueError):
                    continue
                mx = max(mx, th)
    t = mx + 1
    while t in used_thirds or t < 211:
        t += 1
    if t > 253:
        t = 180
        while t in used_thirds and t < 211:
            t += 1
    return t


def _collect_used_veth_thirds_from_kernel() -> Set[int]:
    used: Set[int] = set()
    code, out = _run(["ip", "-j", "addr", "show"])
    if code != 0:
        return used
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return used
    if not isinstance(rows, list):
        return used
    for row in rows:
        if not isinstance(row, dict):
            continue
        for a in row.get("addr_info") or []:
            if not isinstance(a, dict) or a.get("family") != "inet":
                continue
            loc = (a.get("local") or a.get("address") or "").strip()
            if not loc.startswith("10.255."):
                continue
            parts = loc.split(".")
            if len(parts) >= 3:
                try:
                    used.add(int(parts[2]))
                except ValueError:
                    pass
    return used


def _ip_matches_mode(ip_s: str, note: str, mode: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_s)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    if mode == "all":
        return True
    if mode == "note":
        return _note_tag() in (note or "")
    if mode == "cidr":
        try:
            net = ipaddress.ip_network(_match_cidr(), strict=False)
        except ValueError:
            return False
        return ip in net
    return False


def _ip_without_dots(ip_s: str) -> str:
    return ip_s.replace(".", "")


def _vrf_names_for_ip(ip_s: str, prefix: str, vrf_override: Optional[str] = None) -> Tuple[str, str, str, str]:
    parts = ip_s.split(".")
    last = parts[3] if len(parts) == 4 else ""
    ip_nodots = _ip_without_dots(ip_s)
    if vrf_override:
        vrf = vrf_override
        dum = f"dum{last}"
        va = f"{vrf}a"
        vb = f"{vrf}b"
    else:
        vrf = f"{prefix}{ip_nodots}"
        dum = f"dum{last}"
        va = f"vbg{last}a"
        vb = f"vbg{last}b"
    return vrf, dum, va, vb


def _ensure_one_spoof_ip(
    spoof_ip: str,
    phy_vrf: str,
    peer: str,
    prefix: str,
    state: Dict[str, Any],
    used_tables: Set[int],
    vrf_override: Optional[str] = None,
) -> Dict[str, Any]:
    parts = spoof_ip.split(".")
    if len(parts) != 4:
        return {"spoof_ip": spoof_ip, "skipped": True, "reason": "not_ipv4"}
    try:
        last = int(parts[3])
    except ValueError:
        return {"spoof_ip": spoof_ip, "skipped": True, "reason": "bad_octet"}
    if last < 0 or last > 255:
        return {"spoof_ip": spoof_ip, "skipped": True, "reason": "bad_octet"}

    vrf, dum, va, vb = _vrf_names_for_ip(spoof_ip, prefix, vrf_override)
    by = state.setdefault("by_spoof_ip", {})
    if not isinstance(by, dict):
        state["by_spoof_ip"] = {}
        by = state["by_spoof_ip"]

    existing_other = [k for k in by if k != spoof_ip and isinstance(by.get(k), dict) and (by[k].get("vrf") or "") == vrf]
    if existing_other:
        return {
            "spoof_ip": spoof_ip,
            "skipped": True,
            "reason": f"vrf_name_collision:{vrf}",
            "conflict_with": existing_other[0],
        }

    row = by.get(spoof_ip)
    if isinstance(row, dict) and row.get("table") is not None and row.get("veth_third") is not None:
        table = int(row["table"])
        veth_third = int(row["veth_third"])
    else:
        table = _pick_table(last, used_tables)
        used_thirds = _collect_used_veth_thirds_from_kernel()
        for _k, v in by.items():
            if isinstance(v, dict) and v.get("veth_third") is not None:
                try:
                    used_thirds.add(int(v["veth_third"]))
                except (TypeError, ValueError):
                    pass
        veth_third = _next_veth_third(state, used_thirds)
        by[spoof_ip] = {"vrf": vrf, "table": table, "veth_third": veth_third, "last_octet": last}
        row = by[spoof_ip]

    table = int(row["table"])
    veth_third = int(row["veth_third"])
    used_tables.add(table)

    o1 = f"10.255.{veth_third}.1"
    o2 = f"10.255.{veth_third}.2"

    if _iface_exists(vrf):
        logger.info("satellite_vrf_assign: vrf exists, refresh host route vrf=%s -> %s", vrf, peer)
        route_rc = -1
        route_err = ""
        if _assign_ip_cmds():
            route_rc, route_out = _run(["ip", "route", "replace", "vrf", vrf, f"{peer}/32", "via", o2, "dev", va])
            if route_rc != 0:
                route_err = (route_out or "")[:400]
                logger.warning("satellite_vrf_assign: refresh route vrf %s: %s", vrf, route_err)
        return {
            "spoof_ip": spoof_ip,
            "vrf": vrf,
            "table": table,
            "veth_third": veth_third,
            "underlay_local": o1,
            "skipped": True,
            "reason": "already_exists",
            "route_rc": route_rc,
            "route_error": route_err or None,
        }

    if not _assign_ip_cmds():
        return {"spoof_ip": spoof_ip, "vrf": vrf, "table": table, "dry_run": True}

    _run(["modprobe", "dummy"])

    if not _phy_iface_ok(phy_vrf):
        return {"spoof_ip": spoof_ip, "skipped": True, "reason": f"phy_vrf_missing:{phy_vrf}"}

    _run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    _run(["sysctl", "-w", "net.ipv4.tcp_l3mdev=1"])
    _run(["sysctl", "-w", "net.ipv4.udp_l3mdev_accept=1"])
    for i in ("all", "default"):
        _run(["sysctl", "-w", f"net.ipv4.conf.{i}.rp_filter=2"])
    if not _phy_is_main(phy_vrf):
        _run(["sysctl", "-w", f"net.ipv4.conf.{phy_vrf}.rp_filter=2"])

    rc, out = _run(["ip", "link", "add", vrf, "type", "vrf", "table", str(table)])
    if rc != 0 and "File exists" not in out:
        logger.warning("satellite_vrf_assign: add vrf %s: %s", vrf, out[:500])
        return {"spoof_ip": spoof_ip, "error": out[:400]}
    _run(["ip", "link", "set", vrf, "up"])

    if not _iface_exists(dum):
        _run(["ip", "link", "add", dum, "type", "dummy"])
    _run(["ip", "link", "set", dum, "master", vrf])
    _run(["ip", "addr", "flush", "dev", dum])
    _run(["ip", "addr", "add", f"{spoof_ip}/32", "dev", dum])
    _run(["ip", "link", "set", dum, "up"])
    _run(["sysctl", "-w", f"net.ipv4.conf.{dum}.rp_filter=2"])

    if not _iface_exists(va):
        rc2, out2 = _run(["ip", "link", "add", va, "type", "veth", "peer", "name", vb])
        if rc2 != 0:
            logger.warning("satellite_vrf_assign: veth %s: %s", va, out2[:500])
            return {"spoof_ip": spoof_ip, "error": out2[:400]}
    _run(["ip", "link", "set", va, "master", vrf])
    if _phy_is_main(phy_vrf):
        _run(["ip", "link", "set", vb, "nomaster"])
    else:
        _run(["ip", "link", "set", vb, "master", phy_vrf])
    _run(["ip", "addr", "flush", "dev", va])
    _run(["ip", "addr", "flush", "dev", vb])
    _run(["ip", "addr", "add", f"{o1}/30", "dev", va])
    _run(["ip", "addr", "add", f"{o2}/30", "dev", vb])
    _run(["ip", "link", "set", va, "up"])
    _run(["ip", "link", "set", vb, "up"])
    _run(["sysctl", "-w", f"net.ipv4.conf.{va}.rp_filter=2"])
    _run(["sysctl", "-w", f"net.ipv4.conf.{vb}.rp_filter=2"])

    _run(["ip", "route", "add", "vrf", vrf, "default", "dev", va])

    rc3, out3 = _run(["ip", "route", "replace", "vrf", vrf, f"{peer}/32", "via", o2, "dev", va])
    if rc3 != 0:
        logger.warning("satellite_vrf_assign: route vrf %s -> %s: %s", vrf, peer, out3[:500])
        return {"spoof_ip": spoof_ip, "vrf": vrf, "route_error": out3[:400]}

    # 添加策略路由规则
    # 使用合理的优先级范围，确保与其他卫星VRF的优先级一致
    priority = 1000 + (last % 64)
    _run(["ip", "rule", "add", "from", spoof_ip, "lookup", str(table), "priority", str(priority)])
    logger.info("satellite_vrf_assign: added policy route for %s -> table %d (priority %d)", spoof_ip, table, priority)

    # 在 phy（主表或命名 VRF）中添加到冒充 IP 的 /32，便于从现网侧访问 dummy 上的地址
    prc, pout = _phy_route_replace_spoof(spoof_ip, o1, vb, phy_vrf)
    if prc != 0:
        logger.warning("satellite_vrf_assign: phy host route %s: %s", spoof_ip, (pout or "")[:400])
    logger.info("satellite_vrf_assign: added route in phy=%s to %s via %s rc=%s", phy_vrf, spoof_ip, o1, prc)

    return {"spoof_ip": spoof_ip, "vrf": vrf, "table": table, "veth_third": veth_third, "ok": True}


def satellite_spoof_ip_tracked(db_path: Path, spoof_ip: str) -> bool:
    ip_n = (spoof_ip or "").strip()
    if not ip_n:
        return False
    st = _load_state(state_path(Path(db_path).expanduser()))
    by = st.get("by_spoof_ip") or {}
    if not isinstance(by, dict):
        return False
    return isinstance(by.get(ip_n), dict)


def underlay_local_ip_for_vrf(vrf: str, db_path: Path) -> Optional[str]:
    vrf_n = (vrf or "").strip()
    if not vrf_n:
        return None
    st = _load_state(state_path(Path(db_path).expanduser()))
    by = st.get("by_spoof_ip") or {}
    if not isinstance(by, dict):
        return None
    for _spoof, row in by.items():
        if not isinstance(row, dict):
            continue
        if (row.get("vrf") or "").strip() != vrf_n:
            continue
        vt = row.get("veth_third")
        if vt is None:
            return None
        try:
            third = int(vt)
        except (TypeError, ValueError):
            return None
        return f"10.255.{third}.1"
    return None


def _remove_one_spoof_ip(
    spoof_ip: str,
    prefix: str,
    state: Dict[str, Any],
    vrf_override: str = "",
) -> Dict[str, Any]:
    parts = spoof_ip.split(".")
    if len(parts) != 4:
        return {"spoof_ip": spoof_ip, "skipped": True, "reason": "not_ipv4"}
    try:
        last = int(parts[3])
    except ValueError:
        return {"spoof_ip": spoof_ip, "skipped": True, "reason": "bad_octet"}
    if last < 0 or last > 255:
        return {"spoof_ip": spoof_ip, "skipped": True, "reason": "bad_octet"}

    vrf, dum, va, vb = _vrf_names_for_ip(spoof_ip, prefix, vrf_override if vrf_override else None)
    by = state.get("by_spoof_ip") or {}
    
    vrf_exists = _iface_exists(vrf)
    
    if not vrf_exists:
        if spoof_ip in by:
            del by[spoof_ip]
        return {"spoof_ip": spoof_ip, "skipped": True, "reason": "vrf_not_found", "vrf": vrf}

    try:
        table = int(by.get(spoof_ip, {}).get("table", 0)) if isinstance(by.get(spoof_ip), dict) else 0
        
        if table == 0:
            table = 30000 + last + 40
        
        # 删除策略路由规则
        _run(["ip", "rule", "del", "from", spoof_ip, "lookup", str(table)])

        phy_nm = _phy_vrf()
        br = by.get(spoof_ip) if isinstance(by.get(spoof_ip), dict) else {}
        vt = 0
        if isinstance(br, dict) and br.get("veth_third") is not None:
            try:
                vt = int(br["veth_third"])
            except (TypeError, ValueError):
                vt = 0
        if vt > 0:
            o1 = f"10.255.{vt}.1"
            _phy_route_del_spoof(spoof_ip, o1, vb, phy_nm)
        
        if _iface_exists(vrf):
            _run(["ip", "link", "del", vrf])
        
        if _iface_exists(dum):
            _run(["ip", "link", "del", dum])
        
        if _iface_exists(va):
            _run(["ip", "link", "del", va])
        
        if _iface_exists(vb):
            _run(["ip", "link", "del", vb])
        
        if spoof_ip in by:
            del by[spoof_ip]
        
        logger.info("satellite_vrf_assign: removed satellite VRF for %s", spoof_ip)
        return {"spoof_ip": spoof_ip, "vrf": vrf, "deleted": True}
        
    except Exception as e:
        logger.exception("satellite_vrf_assign: remove %s failed", spoof_ip)
        return {"spoof_ip": spoof_ip, "error": str(e)[:400]}


def reconcile_from_op_database(db_path: Path) -> Dict[str, Any]:
    if _assign_disabled():
        return {"skipped": True, "reason": "MTR_AUTO_SATELLITE_VRF off or invalid"}

    mode = _mode()
    db_path = Path(db_path).expanduser()
    if not db_path.is_file():
        return {"skipped": True, "reason": "no_db"}

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        row = conn.execute("SELECT arp_spoof_enabled FROM arp_spoof_settings WHERE id = 1").fetchone()
        arp_on = bool(row and int(row[0] or 0))
        if not arp_on:
            return {"skipped": True, "reason": "arp_spoof_disabled"}

        rows = conn.execute(
            "SELECT spoof_gateway_ip, note, satellite_vrf FROM arp_spoof_targets WHERE enabled = 1 ORDER BY id ASC"
        ).fetchall()
    except sqlite3.OperationalError as e:
        return {"skipped": True, "reason": str(e)}
    finally:
        conn.close()

    phy = _phy_vrf()
    peer = _peer_ip()
    prefix = _vrf_prefix()
    st_path = state_path(db_path)
    state = _load_state(st_path)

    vrf_map = _list_kernel_vrf_tables()
    used_tables: Set[int] = set(vrf_map.values())

    current_spoof_ips: Set[str] = set()
    current_spoof_vrfs: Dict[str, str] = {}
    for r in rows:
        ip_s = (r[0] or "").strip()
        note = str(r[1] or "")
        vrf_s = (r[2] or "").strip() if len(r) > 2 else ""
        if not ip_s or not _ip_matches_mode(ip_s, note, mode):
            continue
        current_spoof_ips.add(ip_s)
        if vrf_s:
            current_spoof_vrfs[ip_s] = vrf_s

    deleted_results: List[Dict[str, Any]] = []
    by = state.get("by_spoof_ip") or {}
    if isinstance(by, dict):
        for spoof_ip in list(by.keys()):
            if spoof_ip not in current_spoof_ips:
                vrf_override = current_spoof_vrfs.get(spoof_ip, "")
                result = _remove_one_spoof_ip(spoof_ip, prefix, state, vrf_override)
                deleted_results.append(result)

    results: List[Dict[str, Any]] = []
    for r in rows:
        ip_s = (r[0] or "").strip()
        note = str(r[1] or "")
        vrf_s = (r[2] or "").strip() if len(r) > 2 else ""
        if not ip_s or not _ip_matches_mode(ip_s, note, mode):
            continue
        one = _ensure_one_spoof_ip(ip_s, phy, peer, prefix, state, used_tables, vrf_s if vrf_s else None)
        results.append(one)

    _save_state(st_path, state)
    return {"mode": mode, "phy_vrf": phy, "peer": peer, "prefix": prefix, "results": results, "deleted": deleted_results, "count": len(results)}


def reconcile_best_effort(db_path: Path) -> None:
    try:
        r = reconcile_from_op_database(db_path)
        logger.info("satellite_vrf_assign: %s", r)
    except Exception:
        logger.exception("satellite_vrf_assign reconcile failed")
