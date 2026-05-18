"""Reconcile BGP satellite VRFs with ipvlan L2 interfaces.

This module implements the Linux 200 side of the fixed Linux 201 topology:
Linux 201 keeps static neighbors such as 10.133.152.250, while Linux 200
places that source address inside a matching VRF on an ipvlan L2 interface
over the physical 152-facing interface.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .vrf_naming import satellite_vrf_name

logger = logging.getLogger(__name__)

_STATE_NAME = ".bgp_ipvlan_reconcile.json"
_IFNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,14}$")


def enabled() -> bool:
    raw = (os.environ.get("MTR_BGP_IPVLAN_AUTO") or "1").strip().lower()
    return raw not in {"", "0", "off", "false", "no", "none"}


def peer_ip() -> str:
    """已弃用：VRF 路由对端请用 ``peer_ip_for_vrf``（读 BGP 管理邻居）。"""
    return (
        os.environ.get("MTR_BGP_IPVLAN_PEER_IP")
        or os.environ.get("MTR_SATELLITE_PEER_IP")
        or ""
    ).strip()


def peer_ip_for_vrf(
    db_path: Path,
    vrf: str,
    peer_ip: Optional[str] = None,
) -> Optional[str]:
    """
    解析卫星 VRF 的 BGP 对端 IP：优先调用方传入（BGP 管理新增邻居），
    否则读 ``bgp_neighbor_meta`` 中该 VRF 的下游邻居。
    """
    if peer_ip and str(peer_ip).strip():
        try:
            return str(ipaddress.ip_address(str(peer_ip).strip()))
        except ValueError:
            return None
    db_path = Path(db_path).expanduser()
    if not db_path.is_file():
        return None
    from . import storage

    conn = storage.connect(db_path)
    try:
        return storage.downstream_neighbor_ip_for_vrf(conn, (vrf or "").strip())
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def vrf_prefix() -> str:
    p = (os.environ.get("MTR_SATELLITE_VRF_PREFIX") or "vbgp").strip()
    return p if p else "vbgp"


def state_path(db_path: Path) -> Path:
    return Path(db_path).expanduser().parent / _STATE_NAME


def _run(cmd: List[str], timeout: int = 20) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {"by_spoof_ip": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"by_spoof_ip": {}}
        if not isinstance(raw.get("by_spoof_ip"), dict):
            raw["by_spoof_ip"] = {}
        return raw
    except (OSError, json.JSONDecodeError, TypeError):
        return {"by_spoof_ip": {}}


def _save_state(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=0, sort_keys=True), encoding="utf-8")
    except OSError as e:
        logger.warning("bgp_ipvlan_reconcile: save state %s: %s", path, e)


def _iface_exists(name: str) -> bool:
    rc, _ = _run(["ip", "link", "show", "dev", name], timeout=8)
    return rc == 0


def _kernel_vrf_tables() -> Dict[str, int]:
    rc, out = _run(["ip", "-j", "link", "show", "type", "vrf"], timeout=8)
    if rc != 0 or not out.strip():
        return {}
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return {}
    ret: Dict[str, int] = {}
    if not isinstance(rows, list):
        return ret
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = (row.get("ifname") or "").strip()
        linkinfo = row.get("linkinfo") if isinstance(row.get("linkinfo"), dict) else {}
        if (linkinfo.get("info_kind") or row.get("link_type") or "").strip().lower() != "vrf":
            continue
        data = linkinfo.get("info_data") if isinstance(linkinfo.get("info_data"), dict) else {}
        try:
            table = int(data.get("table") or 0)
        except (TypeError, ValueError):
            table = 0
        if name and table > 0:
            ret[name] = table
    return ret


def _valid_ifname(name: str) -> bool:
    return bool(_IFNAME_RE.match((name or "").strip()))


def _last_octet(ip_s: str) -> Optional[int]:
    try:
        ip = ipaddress.ip_address(ip_s)
    except ValueError:
        return None
    if ip.version != 4:
        return None
    return int(str(ip).split(".")[-1])


def _peer_cidr_for(peer_norm: str) -> str:
    raw = (os.environ.get("MTR_BGP_IPVLAN_PEER_CIDR") or "").strip()
    if raw:
        return str(ipaddress.ip_network(raw, strict=False))
    try:
        p = ipaddress.ip_address(peer_norm)
        if p.version == 4 and p.is_private is False:
            o = int(p.packed[1])
            if o == 133 and int(p.packed[2]) == 152:
                return "10.133.152.0/24"
    except ValueError:
        pass
    return str(ipaddress.ip_network(f"{peer_norm}/24", strict=False))


def _purge_stale_vrf_routes(vrf: str, peer_norm: str, peer_cidr: str) -> List[Dict[str, Any]]:
    """删除 vrf 内 legacy veth/错误 default，以及与当前 peer 不一致的 152 网段路由。"""
    deleted: List[Dict[str, Any]] = []
    rc, out = _run(["ip", "route", "show", "vrf", vrf], timeout=8)
    if rc != 0:
        return deleted
    keep_host = f"{peer_norm}/32"
    keep = {keep_host, peer_cidr}
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        dst = parts[0]
        if dst in keep:
            continue
        stale = False
        if dst == "default":
            stale = True
        elif "10.255." in line or "vrftrans" in line or " dum" in f" {line} ":
            stale = True
        elif dst.startswith("10.133.152.") or (
            dst.endswith("/32") and dst.split("/")[0] != peer_norm and "10.133.152." in dst
        ):
            stale = True
        if not stale:
            continue
        drc, dout = _run(["ip", "route", "del", "vrf", vrf, dst], timeout=8)
        deleted.append({"dst": dst, "line": line.strip()[:120], "rc": drc, "error": dout[:200] if drc != 0 else ""})
    return deleted


def _read_enabled_satellite_rows(db_path: Path) -> List[Dict[str, str]]:
    """已启用且带卫星 VRF 的 ARP 行（``satellite_vrf`` 或备注含 ``BGPSAT``）。"""
    db_path = Path(db_path).expanduser()
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        rows = conn.execute(
            "SELECT spoof_gateway_ip, egress_iface, satellite_vrf, note "
            "FROM arp_spoof_targets WHERE enabled = 1 ORDER BY id ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    out: List[Dict[str, str]] = []
    for ip_s, iface, vrf, note in rows:
        ip_n = (ip_s or "").strip()
        if not ip_n:
            continue
        try:
            ip_n = str(ipaddress.ip_address(ip_n))
        except ValueError:
            continue
        vrf_n = (vrf or "").strip()
        if not vrf_n and "BGPSAT" in str(note or "").upper():
            vrf_n = satellite_vrf_name(ip_n)
        if not vrf_n:
            continue
        out.append(
            {
                "spoof_ip": ip_n,
                "base_iface": (iface or os.environ.get("MTR_BGP_IPVLAN_BASE_IFACE") or "ens192").strip(),
                "vrf": vrf_n,
            }
        )
    return out


def source_ip_for_vrf(db_path: Path, vrf: str) -> Optional[str]:
    vrf_n = (vrf or "").strip()
    if not vrf_n:
        return None
    for row in _read_enabled_satellite_rows(db_path):
        if row["vrf"] == vrf_n:
            return row["spoof_ip"]
    return None


def ipvlan_iface_for_vrf(db_path: Path, vrf: str) -> Optional[str]:
    """卫星 VRF 对应 ipvlan 口名（如 iv249），供 GoBGP ``bind_interface`` 使用。"""
    vrf_n = (vrf or "").strip()
    if not vrf_n:
        return None
    db_path = Path(db_path).expanduser()
    for row in _read_enabled_satellite_rows(db_path):
        if row["vrf"] != vrf_n:
            continue
        last = _last_octet(row["spoof_ip"])
        if last is None:
            return None
        pfx = (os.environ.get("MTR_BGP_IPVLAN_IF_PREFIX") or "iv").strip()
        name = f"{pfx}{last}"
        return name if _valid_ifname(name) else None
    st = _load_state(state_path(db_path))
    by = st.get("by_spoof_ip")
    if isinstance(by, dict):
        for _ip, row in by.items():
            if isinstance(row, dict) and (row.get("vrf") or "").strip() == vrf_n:
                iv = (row.get("ipvlan") or "").strip()
                if iv and _valid_ifname(iv):
                    return iv
    return None


def _policy_rule_priority(last: int) -> int:
    return 1000 + (int(last) % 64)


def _rr_local_bgp_ip() -> str:
    return (os.environ.get("ROUTER_ID") or "139.159.43.207").strip()


def _rr_uplink_iface() -> str:
    """真 RR 所在二层口（与卫星 ipvlan 父口 eno1np0 分离）。"""
    return (os.environ.get("MTR_BGP_RR_UPLINK_IFACE") or "enp59s0f0np0").strip()


def _should_policy_route_spoof(spoof_ip: str) -> bool:
    """RR 本端地址（207）须走主表连真 RR，不能 ``from 207 lookup`` 卫星表。"""
    try:
        return str(ipaddress.ip_address(spoof_ip)) != str(ipaddress.ip_address(_rr_local_bgp_ip()))
    except ValueError:
        return True


def satellite_spoof_ip_tracked(db_path: Path, spoof_ip: str) -> bool:
    ip_n = (spoof_ip or "").strip()
    if not ip_n:
        return False
    for row in _read_enabled_satellite_rows(db_path):
        if row["spoof_ip"] == ip_n:
            return True
    st = _load_state(state_path(Path(db_path).expanduser()))
    by = st.get("by_spoof_ip") or {}
    return isinstance(by, dict) and isinstance(by.get(ip_n), dict)


def rr_spoof_ip() -> str:
    return (os.environ.get("MTR_FORM_RR_HINT") or "139.159.43.249").strip()


def is_rr_spoof_ip(ip: str) -> bool:
    try:
        return str(ipaddress.ip_address((ip or "").strip())) == str(ipaddress.ip_address(rr_spoof_ip()))
    except ValueError:
        return False


def rr_spoof_passive_enabled() -> bool:
    return os.environ.get("MTR_BGP_RR_SPOOF_PASSIVE", "1").strip().lower() not in {"0", "false", "no"}


def tx_listen_port_for_vrf(vrf: str, base: int = 1790) -> int:
    """与 ``bgp_agent/pkg/tx/pool.go`` 的 ``portFor`` 算法一致（uint16 溢出）。"""
    if not vrf or vrf == "default":
        return base
    h = 0
    for ch in vrf:
        h = (h * 31 + ord(ch)) & 0xFFFF
    return base + 1 + (h % 50)


_DNAT_TABLE = "mtr_bgp_sat_dnat"
_LEGACY_RR_DNAT_TABLE = "mtr_bgp_spoof_rr"


def satellite_dnat_enabled() -> bool:
    if not enabled():
        return False
    raw = (os.environ.get("MTR_BGP_SAT_DNAT_AUTO") or "1").strip().lower()
    return raw not in {"", "0", "off", "false", "no", "none"}


def _satellite_dnat_use_iif() -> bool:
    return os.environ.get("MTR_BGP_SAT_DNAT_IIF", "").strip().lower() in {"1", "true", "yes", "on"}


def reconcile_satellite_dnat(db_path: Path) -> Dict[str, Any]:
    """
    为所有卫星冒充 IP 安装入站 :179 → TX 监听端口的 nft redirect（201 主动连标准 179 时必需）。
    按库中 ARP 行全量重建 prerouting 链，增删 ARP/BGP 后调用即可。
    """
    if not satellite_dnat_enabled():
        return {"skipped": True, "reason": "MTR_BGP_SAT_DNAT_AUTO off"}
    db_path = Path(db_path).expanduser()
    rows = _read_enabled_satellite_rows(db_path)
    steps: List[Dict[str, Any]] = []

    def step(name: str, cmd: List[str]) -> bool:
        rc, out = _run(cmd, timeout=12)
        ok = rc == 0 or "exists" in (out or "").lower() or "No such" in (out or "")
        steps.append({"name": name, "cmd": cmd, "rc": rc, "ok": ok, "error": out[:200] if not ok else ""})
        return ok

    step("drop_legacy_rr_table", ["nft", "delete", "table", "inet", _LEGACY_RR_DNAT_TABLE])
    if not rows:
        step("drop_dnat_table", ["nft", "delete", "table", "inet", _DNAT_TABLE])
        return {"ok": True, "rules": [], "steps": steps}

    step("nft_table", ["nft", "add", "table", "inet", _DNAT_TABLE])
    # 链定义须为单个参数，否则 ``priority -100`` 会被拆成 ``-1`` 与 ``00`` 导致 nft 报错
    step(
        "nft_chain",
        [
            "nft",
            "add",
            "chain",
            "inet",
            _DNAT_TABLE,
            "prerouting",
            "{ type nat hook prerouting priority -100; policy accept; }",
        ],
    )
    step("nft_flush", ["nft", "flush", "chain", "inet", _DNAT_TABLE, "prerouting"])
    use_iif = _satellite_dnat_use_iif()
    rules: List[Dict[str, Any]] = []
    for row in rows:
        spoof_ip = row["spoof_ip"]
        vrf = row["vrf"] or satellite_vrf_name(spoof_ip)
        port = tx_listen_port_for_vrf(vrf)
        base_iface = (row.get("base_iface") or "").strip()
        rule_cmd: List[str] = [
            "nft",
            "add",
            "rule",
            "inet",
            _DNAT_TABLE,
            "prerouting",
        ]
        if use_iif and base_iface:
            rule_cmd.extend(["iifname", base_iface])
        rule_cmd.extend(
            [
                "ip",
                "daddr",
                spoof_ip,
                "tcp",
                "dport",
                "179",
                "redirect",
                "to",
                f":{port}",
            ]
        )
        ok = step(f"redirect_{spoof_ip}", rule_cmd)
        rules.append(
            {
                "spoof_ip": spoof_ip,
                "vrf": vrf,
                "redirect_port": port,
                "iif": base_iface if use_iif else "",
                "ok": ok,
            }
        )
    return {"ok": all(r.get("ok", True) for r in rules), "rules": rules, "steps": steps}


def ensure_rr_spoof_dnat(
    spoof_ip: str,
    vrf: str,
    base_iface: str,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """兼容旧调用：按库全量重建卫星 DNAT（RR 与其它卫星 IP 共用 ``mtr_bgp_sat_dnat``）。"""
    del vrf, base_iface
    if not is_rr_spoof_ip(spoof_ip):
        return {"skipped": True, "reason": "not_rr_spoof_ip"}
    if db_path is None:
        return {"skipped": True, "reason": "no_db_path"}
    return reconcile_satellite_dnat(db_path)


def _ensure_peer_static_neigh(peer: str, iv: str, base_iface: str) -> Dict[str, Any]:
    """对端不响应 ARP 时，用同网段已学习邻居 MAC 写永久邻居（本端修复）。"""
    rc, out = _run(["ip", "neigh", "show", peer, "dev", iv], timeout=8)
    if rc == 0 and out.strip() and "FAILED" not in out and "INCOMPLETE" not in out:
        return {"peer": peer, "skipped": True, "reason": "neigh_ok", "neigh": out.strip()[:120]}
    env_key = f"MTR_BGP_PEER_NEIGH_MAC_{peer.replace('.', '_')}"
    mac = (os.environ.get(env_key) or os.environ.get("MTR_BGP_PEER_NEIGH_MAC") or "").strip()
    if not mac:
        rc2, out2 = _run(["ip", "neigh", "show", "dev", base_iface], timeout=8)
        if rc2 == 0:
            for alt in ("139.159.43.204", "139.159.43.206", "139.159.43.205"):
                if alt == peer:
                    continue
                for line in out2.splitlines():
                    if alt in line and "lladdr" in line:
                        parts = line.split()
                        try:
                            mac = parts[parts.index("lladdr") + 1]
                        except (ValueError, IndexError):
                            mac = ""
                        if mac:
                            break
                if mac:
                    break
    if not mac:
        return {"peer": peer, "skipped": True, "reason": "no_mac_candidate"}
    rc3, out3 = _run(
        ["ip", "neigh", "replace", peer, "lladdr", mac, "dev", iv, "nud", "permanent"],
        timeout=8,
    )
    return {
        "peer": peer,
        "mac": mac,
        "ok": rc3 == 0,
        "error": out3[:200] if rc3 != 0 else "",
    }


def _delete_rules_for_spoof(spoof_ip: str) -> List[Dict[str, Any]]:
    rc, out = _run(["ip", "-4", "rule", "show"], timeout=8)
    if rc != 0:
        return [{"error": out[:300]}]
    deleted: List[Dict[str, Any]] = []
    for line in out.splitlines():
        if spoof_ip not in line:
            continue
        if f"from {spoof_ip}" not in line and f"to {spoof_ip}" not in line:
            continue
        pref = line.split(":", 1)[0].strip()
        if not pref.isdigit():
            continue
        drc, dout = _run(["ip", "-4", "rule", "del", "pref", pref], timeout=8)
        deleted.append({"pref": int(pref), "rc": drc, "error": dout[:200] if drc != 0 else ""})
    return deleted


def remove_spoof_ipvlan_l2(db_path: Path, spoof_ip: str, vrf: str = "") -> Dict[str, Any]:
    """删除单条冒充 IP 的 ipvlan、策略路由规则，并更新 reconcile 状态文件。"""
    if not enabled():
        return {"skipped": True, "reason": "MTR_BGP_IPVLAN_AUTO off"}
    try:
        ip_n = str(ipaddress.ip_address((spoof_ip or "").strip()))
    except ValueError:
        return {"skipped": True, "reason": "not_ipv4"}
    last = _last_octet(ip_n)
    db_path = Path(db_path).expanduser()
    st_path = state_path(db_path)
    state = _load_state(st_path)
    by = state.get("by_spoof_ip")
    if not isinstance(by, dict):
        by = {}
        state["by_spoof_ip"] = by
    state_row = by.get(ip_n) if isinstance(by.get(ip_n), dict) else {}
    pfx = (os.environ.get("MTR_BGP_IPVLAN_IF_PREFIX") or "iv").strip()
    iv = (state_row.get("ipvlan") if isinstance(state_row, dict) else "") or (f"{pfx}{last}" if last is not None else "")
    steps: List[Dict[str, Any]] = []
    rule_del = _delete_rules_for_spoof(ip_n)
    if rule_del:
        steps.append({"name": "del_policy_rules", "deleted": rule_del})
    if iv and _valid_ifname(iv) and _iface_exists(iv):
        rc, out = _run(["ip", "link", "del", iv], timeout=12)
        steps.append({"name": "del_ipvlan", "cmd": ["ip", "link", "del", iv], "rc": rc, "ok": rc == 0, "error": out[:200]})
    vrf_n = (vrf or (state_row.get("vrf") if isinstance(state_row, dict) else "") or "").strip()
    if vrf_n and _iface_exists(vrf_n):
        _run(["ip", "route", "flush", "vrf", vrf_n], timeout=8)
    if ip_n in by:
        del by[ip_n]
    _save_state(st_path, state)
    return {"ok": True, "spoof_ip": ip_n, "ipvlan": iv, "vrf": vrf_n, "steps": steps}


def _purge_orphan_spoof_state(
    db_path: Path,
    state: Dict[str, Any],
    rows: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    current = {r["spoof_ip"] for r in rows}
    by = state.get("by_spoof_ip")
    if not isinstance(by, dict):
        return []
    removed: List[Dict[str, Any]] = []
    for ip in list(by.keys()):
        if ip in current:
            continue
        vrf = (by.get(ip) or {}).get("vrf", "") if isinstance(by.get(ip), dict) else ""
        removed.append(remove_spoof_ipvlan_l2(db_path, ip, vrf=str(vrf)))
    return removed


def _alloc_table(vrf: str, last: int, used_tables: Set[int], state_row: Dict[str, Any]) -> int:
    vrf_tables = _kernel_vrf_tables()
    if vrf in vrf_tables:
        return int(vrf_tables[vrf])
    try:
        t0 = int(state_row.get("table") or 0)
    except (TypeError, ValueError):
        t0 = 0
    if t0 > 0 and t0 not in used_tables:
        used_tables.add(t0)
        return t0
    cand = 30200 + int(last)
    if cand not in used_tables:
        used_tables.add(cand)
        return cand
    for t in range(30200, 65000):
        if t not in used_tables:
            used_tables.add(t)
            return t
    return 65001


def _ensure_one(
    row: Dict[str, str],
    state: Dict[str, Any],
    used_tables: Set[int],
    db_path: Path,
    peer_ip: Optional[str] = None,
) -> Dict[str, Any]:
    spoof_ip = row["spoof_ip"]
    base_iface = row["base_iface"] or (os.environ.get("MTR_BGP_IPVLAN_BASE_IFACE") or "ens192").strip()
    vrf = row["vrf"]
    last = _last_octet(spoof_ip)
    if last is None:
        return {"spoof_ip": spoof_ip, "skipped": True, "reason": "not_ipv4"}
    if not vrf:
        vrf = satellite_vrf_name(spoof_ip)
    if not _valid_ifname(vrf):
        return {"spoof_ip": spoof_ip, "vrf": vrf, "error": "invalid_vrf_ifname"}
    iv = (os.environ.get("MTR_BGP_IPVLAN_IF_PREFIX") or "iv").strip() + str(last)
    if not _valid_ifname(iv):
        return {"spoof_ip": spoof_ip, "vrf": vrf, "error": "invalid_ipvlan_ifname", "iface": iv}
    if not base_iface or not _iface_exists(base_iface):
        return {"spoof_ip": spoof_ip, "vrf": vrf, "error": f"base_iface_missing:{base_iface}"}

    by = state.setdefault("by_spoof_ip", {})
    if not isinstance(by, dict):
        state["by_spoof_ip"] = {}
        by = state["by_spoof_ip"]
    state_row = by.get(spoof_ip)
    if not isinstance(state_row, dict):
        state_row = {}
        by[spoof_ip] = state_row
    table = _alloc_table(vrf, last, used_tables, state_row)
    state_row.update({"spoof_ip": spoof_ip, "vrf": vrf, "table": table, "base_iface": base_iface, "ipvlan": iv})

    cmds: List[Dict[str, Any]] = []

    def run_step(name: str, cmd: List[str], timeout: int = 20, ignore_exists: bool = False) -> bool:
        rc, out = _run(cmd, timeout=timeout)
        ok = rc == 0 or (ignore_exists and "exists" in out.lower())
        cmds.append({"name": name, "cmd": cmd, "rc": rc, "ok": ok, "error": out[:300] if not ok else ""})
        return ok

    run_step("rp_filter_all", ["sysctl", "-w", "net.ipv4.conf.all.rp_filter=0"], timeout=8)
    run_step("rp_filter_default", ["sysctl", "-w", "net.ipv4.conf.default.rp_filter=0"], timeout=8)
    run_step("rp_filter_base", ["sysctl", "-w", f"net.ipv4.conf.{base_iface}.rp_filter=0"], timeout=8)
    run_step("tcp_l3mdev_accept", ["sysctl", "-w", "net.ipv4.tcp_l3mdev_accept=1"], timeout=8)
    run_step("udp_l3mdev_accept", ["sysctl", "-w", "net.ipv4.udp_l3mdev_accept=1"], timeout=8)

    if not _iface_exists(vrf):
        if not run_step("add_vrf", ["ip", "link", "add", vrf, "type", "vrf", "table", str(table)], ignore_exists=True):
            return {"spoof_ip": spoof_ip, "vrf": vrf, "error": "add_vrf_failed", "steps": cmds}
    run_step("up_vrf", ["ip", "link", "set", vrf, "up"])

    _delete_rules_for_spoof(spoof_ip)
    if last is not None and _should_policy_route_spoof(spoof_ip):
        run_step(
            "rule_from_spoof",
            [
                "ip",
                "-4",
                "rule",
                "add",
                "from",
                spoof_ip,
                "lookup",
                str(table),
                "priority",
                str(_policy_rule_priority(last)),
            ],
            ignore_exists=True,
        )
    run_step("del_base_duplicate", ["ip", "addr", "del", f"{spoof_ip}/32", "dev", base_iface], timeout=8)
    run_step("del_dummy_duplicate", ["ip", "addr", "del", f"{spoof_ip}/32", "dev", f"dum{last}"], timeout=8)

    if _iface_exists(iv):
        rc_iv, out_iv = _run(["ip", "-j", "link", "show", "dev", iv], timeout=8)
        wrong_parent = False
        if rc_iv == 0 and out_iv.strip():
            try:
                row0 = json.loads(out_iv)[0]
                if isinstance(row0, dict):
                    parent = (row0.get("link") or "").strip()
                    if parent and parent != base_iface:
                        wrong_parent = True
            except (json.JSONDecodeError, IndexError, TypeError):
                pass
        if wrong_parent:
            run_step("del_ipvlan_wrong_parent", ["ip", "link", "del", iv], timeout=8)
    if not _iface_exists(iv):
        if not run_step("add_ipvlan", ["ip", "link", "add", "link", base_iface, "name", iv, "type", "ipvlan", "mode", "l2"]):
            return {"spoof_ip": spoof_ip, "vrf": vrf, "ipvlan": iv, "error": "add_ipvlan_failed", "steps": cmds}
    if not run_step("set_ipvlan_master", ["ip", "link", "set", iv, "master", vrf]):
        return {"spoof_ip": spoof_ip, "vrf": vrf, "ipvlan": iv, "error": "set_master_failed", "steps": cmds}
    run_step("flush_ipvlan_addr", ["ip", "addr", "flush", "dev", iv], timeout=8)
    # 冒充 RR(249) 时不在 iv 上挂 /32，避免本机把 249 当本地地址导致真 RR(207→249) 无法建连；
    # 下游 TX 用 bind_interface + ip_nonlocal_bind + vrf 路由 src=249。
    if is_rr_spoof_ip(spoof_ip):
        run_step("nonlocal_bind", ["sysctl", "-w", "net.ipv4.ip_nonlocal_bind=1"], timeout=8)
        uplink = _rr_uplink_iface()
        if uplink and _iface_exists(uplink):
            run_step(
                "main_rr_host_route",
                ["ip", "route", "replace", f"{spoof_ip}/32", "dev", uplink],
                timeout=8,
            )
    elif not run_step("add_ipvlan_addr", ["ip", "addr", "add", f"{spoof_ip}/32", "dev", iv]):
        return {"spoof_ip": spoof_ip, "vrf": vrf, "ipvlan": iv, "error": "add_ipvlan_addr_failed", "steps": cmds}
    run_step("up_ipvlan", ["ip", "link", "set", iv, "up"])
    run_step("rp_filter_ipvlan", ["sysctl", "-w", f"net.ipv4.conf.{iv}.rp_filter=0"], timeout=8)

    resolved_peer = peer_ip_for_vrf(db_path, vrf, peer_ip)
    if not resolved_peer:
        cmds.append(
            {
                "name": "route_peer_skipped",
                "ok": True,
                "reason": "no_bgp_neighbor_ip:先在 BGP 管理为该 VRF 新增邻居并填写对端 IP",
            }
        )
        return {
            "spoof_ip": spoof_ip,
            "vrf": vrf,
            "table": table,
            "ipvlan": iv,
            "base_iface": base_iface,
            "ok": True,
            "peer_route_pending": True,
            "steps": cmds,
        }
    try:
        peer_norm = str(ipaddress.ip_address(resolved_peer))
        peer_cidr = _peer_cidr_for(peer_norm)
    except ValueError:
        return {"spoof_ip": spoof_ip, "vrf": vrf, "ipvlan": iv, "error": "invalid_peer_ip", "steps": cmds}
    purged = _purge_stale_vrf_routes(vrf, peer_norm, peer_cidr)
    if purged:
        cmds.append({"name": "purge_stale_vrf_routes", "purged": purged})
    run_step(
        "neigh_probe",
        ["ip", "vrf", "exec", vrf, "ping", "-c", "1", "-W", "2", "-I", spoof_ip, peer_norm],
        timeout=8,
    )
    neigh_fix = _ensure_peer_static_neigh(peer_norm, iv, base_iface)
    if neigh_fix:
        cmds.append({"name": "static_neigh", "result": neigh_fix})
    if not run_step("route_peer_host", ["ip", "route", "replace", "vrf", vrf, f"{peer_norm}/32", "dev", iv, "src", spoof_ip]):
        return {"spoof_ip": spoof_ip, "vrf": vrf, "ipvlan": iv, "error": "route_peer_host_failed", "steps": cmds}
    run_step("route_peer_cidr", ["ip", "route", "replace", "vrf", vrf, peer_cidr, "dev", iv, "src", spoof_ip])
    run_step("flush_route_cache", ["ip", "route", "flush", "cache"], timeout=8)

    return {
        "spoof_ip": spoof_ip,
        "vrf": vrf,
        "table": table,
        "ipvlan": iv,
        "base_iface": base_iface,
        "peer_ip": peer_norm,
        "ok": True,
        "steps": cmds,
    }


def _ensure_arp_spoof_global_enabled(db_path: Path) -> bool:
    """冒充 RR 连下游依赖二层 GARP，若 OP 总开关关闭则自动打开。"""
    db_path = Path(db_path).expanduser()
    if not db_path.is_file():
        return False
    rows = _read_enabled_satellite_rows(db_path)
    if not any(is_rr_spoof_ip(r["spoof_ip"]) for r in rows):
        return False
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        cur = conn.execute(
            "UPDATE arp_spoof_settings SET arp_spoof_enabled = 1 "
            "WHERE id = 1 AND COALESCE(arp_spoof_enabled, 0) = 0"
        )
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def reconcile_from_op_database(db_path: Path) -> Dict[str, Any]:
    if not enabled():
        return {"skipped": True, "reason": "MTR_BGP_IPVLAN_AUTO off"}
    db_path = Path(db_path).expanduser()
    if _ensure_arp_spoof_global_enabled(db_path):
        logger.info("bgp_ipvlan: auto-enabled arp_spoof_settings for RR spoof downstream")
    rows = _read_enabled_satellite_rows(db_path)
    st_path = state_path(db_path)
    state = _load_state(st_path)
    dbp = Path(db_path).expanduser()
    if not rows:
        deleted = _purge_orphan_spoof_state(dbp, state, [])
        _save_state(st_path, state)
        dnat = reconcile_satellite_dnat(dbp)
        return {"ok": True, "changed": bool(deleted), "items": [], "deleted": deleted, "dnat": dnat}

    used_tables = set(_kernel_vrf_tables().values())
    deleted = _purge_orphan_spoof_state(dbp, state, rows)
    items = [_ensure_one(row, state, used_tables, dbp) for row in rows]
    _save_state(st_path, state)
    dnat = reconcile_satellite_dnat(dbp)
    return {
        "ok": all(not x.get("error") for x in items) and dnat.get("ok", True) is not False,
        "items": items,
        "deleted": deleted,
        "dnat": dnat,
    }


def reconcile_vrf_from_op_database(
    db_path: Path,
    vrf: str,
    peer_ip: Optional[str] = None,
) -> Dict[str, Any]:
    if not enabled():
        return {"skipped": True, "reason": "MTR_BGP_IPVLAN_AUTO off"}
    vrf_n = (vrf or "").strip()
    dbp = Path(db_path).expanduser()
    rows = [r for r in _read_enabled_satellite_rows(dbp) if r["vrf"] == vrf_n]
    if not rows:
        return {"skipped": True, "reason": f"no_arp_satellite_vrf_row:{vrf_n}"}
    st_path = state_path(dbp)
    state = _load_state(st_path)
    used_tables = set(_kernel_vrf_tables().values())
    items = [_ensure_one(row, state, used_tables, dbp, peer_ip=peer_ip) for row in rows]
    _save_state(st_path, state)
    dnat = reconcile_satellite_dnat(dbp)
    return {
        "ok": all(not x.get("error") for x in items) and dnat.get("ok", True) is not False,
        "items": items,
        "dnat": dnat,
        "peer_known": bool(peer_ip_for_vrf(dbp, vrf_n, peer_ip)),
    }
