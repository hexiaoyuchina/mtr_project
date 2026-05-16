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

logger = logging.getLogger(__name__)

_STATE_NAME = ".bgp_ipvlan_reconcile.json"
_IFNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,14}$")


def enabled() -> bool:
    raw = (os.environ.get("MTR_BGP_IPVLAN_AUTO") or "1").strip().lower()
    return raw not in {"", "0", "off", "false", "no", "none"}


def peer_ip() -> str:
    return (
        os.environ.get("MTR_BGP_IPVLAN_PEER_IP")
        or os.environ.get("MTR_SATELLITE_PEER_IP")
        or "10.133.152.204"
    ).strip()


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


def _ip_without_dots(ip_s: str) -> str:
    return ip_s.replace(".", "")


def _peer_cidr(peer: str) -> str:
    raw = (os.environ.get("MTR_BGP_IPVLAN_PEER_CIDR") or "").strip()
    if raw:
        return str(ipaddress.ip_network(raw, strict=False))
    return str(ipaddress.ip_network(f"{peer}/24", strict=False))


def _read_enabled_satellite_rows(db_path: Path) -> List[Dict[str, str]]:
    db_path = Path(db_path).expanduser()
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        rows = conn.execute(
            "SELECT spoof_gateway_ip, egress_iface, satellite_vrf "
            "FROM arp_spoof_targets WHERE enabled = 1 AND trim(satellite_vrf) <> '' ORDER BY id ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    out: List[Dict[str, str]] = []
    for ip_s, iface, vrf in rows:
        ip_n = (ip_s or "").strip()
        if not ip_n:
            continue
        try:
            ip_n = str(ipaddress.ip_address(ip_n))
        except ValueError:
            continue
        out.append(
            {
                "spoof_ip": ip_n,
                "base_iface": (iface or os.environ.get("MTR_BGP_IPVLAN_BASE_IFACE") or "ens192").strip(),
                "vrf": (vrf or "").strip(),
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


def _ensure_one(row: Dict[str, str], state: Dict[str, Any], used_tables: Set[int]) -> Dict[str, Any]:
    spoof_ip = row["spoof_ip"]
    base_iface = row["base_iface"] or (os.environ.get("MTR_BGP_IPVLAN_BASE_IFACE") or "ens192").strip()
    vrf = row["vrf"]
    last = _last_octet(spoof_ip)
    if last is None:
        return {"spoof_ip": spoof_ip, "skipped": True, "reason": "not_ipv4"}
    if not vrf:
        vrf = f"{vrf_prefix()}{_ip_without_dots(spoof_ip)}"
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
    run_step("del_base_duplicate", ["ip", "addr", "del", f"{spoof_ip}/32", "dev", base_iface], timeout=8)
    run_step("del_dummy_duplicate", ["ip", "addr", "del", f"{spoof_ip}/32", "dev", f"dum{last}"], timeout=8)

    if not _iface_exists(iv):
        if not run_step("add_ipvlan", ["ip", "link", "add", "link", base_iface, "name", iv, "type", "ipvlan", "mode", "l2"]):
            return {"spoof_ip": spoof_ip, "vrf": vrf, "ipvlan": iv, "error": "add_ipvlan_failed", "steps": cmds}
    if not run_step("set_ipvlan_master", ["ip", "link", "set", iv, "master", vrf]):
        return {"spoof_ip": spoof_ip, "vrf": vrf, "ipvlan": iv, "error": "set_master_failed", "steps": cmds}
    run_step("flush_ipvlan_addr", ["ip", "addr", "flush", "dev", iv], timeout=8)
    if not run_step("add_ipvlan_addr", ["ip", "addr", "add", f"{spoof_ip}/32", "dev", iv]):
        return {"spoof_ip": spoof_ip, "vrf": vrf, "ipvlan": iv, "error": "add_ipvlan_addr_failed", "steps": cmds}
    run_step("up_ipvlan", ["ip", "link", "set", iv, "up"])
    run_step("rp_filter_ipvlan", ["sysctl", "-w", f"net.ipv4.conf.{iv}.rp_filter=0"], timeout=8)

    peer = peer_ip()
    try:
        peer_norm = str(ipaddress.ip_address(peer))
        peer_cidr = _peer_cidr(peer_norm)
    except ValueError:
        return {"spoof_ip": spoof_ip, "vrf": vrf, "ipvlan": iv, "error": "invalid_peer_ip"}
    if not run_step("route_peer_host", ["ip", "route", "replace", "vrf", vrf, f"{peer_norm}/32", "dev", iv, "src", spoof_ip]):
        return {"spoof_ip": spoof_ip, "vrf": vrf, "ipvlan": iv, "error": "route_peer_host_failed", "steps": cmds}
    run_step("route_peer_cidr", ["ip", "route", "replace", "vrf", vrf, peer_cidr, "dev", iv, "src", spoof_ip])
    run_step("flush_route_cache", ["ip", "route", "flush", "cache"], timeout=8)

    return {"spoof_ip": spoof_ip, "vrf": vrf, "table": table, "ipvlan": iv, "base_iface": base_iface, "ok": True}


def reconcile_from_op_database(db_path: Path) -> Dict[str, Any]:
    if not enabled():
        return {"skipped": True, "reason": "MTR_BGP_IPVLAN_AUTO off"}
    db_path = Path(db_path).expanduser()
    rows = _read_enabled_satellite_rows(db_path)
    if not rows:
        return {"ok": True, "changed": False, "items": []}

    st_path = state_path(db_path)
    state = _load_state(st_path)
    used_tables = set(_kernel_vrf_tables().values())
    items = [_ensure_one(row, state, used_tables) for row in rows]
    _save_state(st_path, state)
    return {"ok": all(not x.get("error") for x in items), "items": items}


def reconcile_vrf_from_op_database(db_path: Path, vrf: str) -> Dict[str, Any]:
    if not enabled():
        return {"skipped": True, "reason": "MTR_BGP_IPVLAN_AUTO off"}
    vrf_n = (vrf or "").strip()
    rows = [r for r in _read_enabled_satellite_rows(Path(db_path).expanduser()) if r["vrf"] == vrf_n]
    if not rows:
        return {"skipped": True, "reason": f"no_arp_satellite_vrf_row:{vrf_n}"}
    st_path = state_path(Path(db_path).expanduser())
    state = _load_state(st_path)
    used_tables = set(_kernel_vrf_tables().values())
    items = [_ensure_one(row, state, used_tables) for row in rows]
    _save_state(st_path, state)
    return {"ok": all(not x.get("error") for x in items), "items": items}
