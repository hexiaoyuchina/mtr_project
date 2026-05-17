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
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        from . import storage

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


def _ip_without_dots(ip_s: str) -> str:
    return ip_s.replace(".", "")


def _peer_cidr_for(peer_norm: str) -> str:
    raw = (os.environ.get("MTR_BGP_IPVLAN_PEER_CIDR") or "").strip()
    if raw:
        return str(ipaddress.ip_network(raw, strict=False))
    try:
        p = ipaddress.ip_address(peer)
        if p.version == 4 and p.is_private is False:
            o = int(p.packed[1])
            if o == 133 and int(p.packed[2]) == 152:
                return "10.133.152.0/24"
    except ValueError:
        pass
    return str(ipaddress.ip_network(f"{peer}/24", strict=False))


def _purge_stale_vrf_routes(vrf: str, peer_norm: str, peer_cidr: str) -> List[Dict[str, Any]]:
    """删除 vrf 内残留的实验室网段或与当前 peer 不一致的 host 路由。"""
    deleted: List[Dict[str, Any]] = []
    rc, out = _run(["ip", "route", "show", "vrf", vrf], timeout=8)
    if rc != 0:
        return deleted
    keep_host = f"{peer_norm}/32"
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        dst = parts[0]
        if dst in {keep_host, peer_cidr, "default"}:
            continue
        if dst.startswith("10.133.152.") or (
            dst.endswith("/32") and dst.split("/")[0] != peer_norm and "10.133.152." in dst
        ):
            drc, dout = _run(["ip", "route", "del", "vrf", vrf, dst], timeout=8)
            deleted.append({"dst": dst, "rc": drc, "error": dout[:200] if drc != 0 else ""})
    return deleted


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


def ensure_rr_spoof_dnat(spoof_ip: str, vrf: str, base_iface: str) -> Dict[str, Any]:
    """把发往冒充 RR 地址 :179 的入站 TCP 转到卫星 TX 监听端口（避免被 RX :179 复位）。"""
    if not is_rr_spoof_ip(spoof_ip):
        return {"skipped": True, "reason": "not_rr_spoof_ip"}
    port = tx_listen_port_for_vrf(vrf)
    table = "mtr_bgp_spoof_rr"
    iface = (base_iface or "").strip()
    steps: List[Dict[str, Any]] = []

    def step(name: str, cmd: List[str]) -> bool:
        rc, out = _run(cmd, timeout=12)
        ok = rc == 0 or "exists" in (out or "").lower()
        steps.append({"name": name, "cmd": cmd, "rc": rc, "ok": ok, "error": out[:200] if not ok else ""})
        return ok

    step("nft_table", ["nft", "add", "table", "inet", table])
    step(
        "nft_chain",
        [
            "nft",
            "add",
            "chain",
            "inet",
            table,
            "prerouting",
            "{",
            "type",
            "nat",
            "hook",
            "prerouting",
            "priority",
            "-100",
            ";",
            "policy",
            "accept",
            ";",
            "}",
        ],
    )
    step("nft_flush", ["nft", "flush", "chain", "inet", table, "prerouting"])
    # redirect 优于 dnat：目标 249 为本机 iv249 地址时，仍能把入站 :179 转给 TX 监听口
    rule = [
        "nft",
        "add",
        "rule",
        "inet",
        table,
        "prerouting",
        "iifname",
        iface,
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
    step("nft_redirect", rule)
    return {"ok": True, "spoof_ip": spoof_ip, "redirect_port": port, "iface": iface, "steps": steps}


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
    if is_rr_spoof_ip(spoof_ip):
        dnat_rec = ensure_rr_spoof_dnat(spoof_ip, vrf, base_iface)
        cmds.append({"name": "rr_spoof_dnat", "result": dnat_rec})
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
    if not rows:
        return {"ok": True, "changed": False, "items": []}

    st_path = state_path(db_path)
    state = _load_state(st_path)
    used_tables = set(_kernel_vrf_tables().values())
    dbp = Path(db_path).expanduser()
    items = [_ensure_one(row, state, used_tables, dbp) for row in rows]
    _save_state(st_path, state)
    return {"ok": all(not x.get("error") for x in items), "items": items}


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
    if not peer_ip_for_vrf(dbp, vrf_n, peer_ip):
        return {
            "skipped": True,
            "reason": "no_bgp_neighbor_ip",
            "detail": "请先在 BGP 管理为该 VRF 填写邻居 IP 并保存，或新增邻居时一并提交",
        }
    st_path = state_path(dbp)
    state = _load_state(st_path)
    used_tables = set(_kernel_vrf_tables().values())
    items = [_ensure_one(row, state, used_tables, dbp, peer_ip=peer_ip) for row in rows]
    _save_state(st_path, state)
    return {"ok": all(not x.get("error") for x in items), "items": items}
