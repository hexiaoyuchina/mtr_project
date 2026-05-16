"""
ARP 冒充网关 IPv4 在本机「出接口」上的 /32 地址维护。

与 ``scripts/arp_spoof_daemon.py`` 共用：守护进程周期性 reconcile；OP 在写库后**立即**调用一次，
避免仅依赖守护进程轮询时出现数秒空窗（BGP ``update-source`` 等会立刻需要本机地址）。

撤下某条 ``(egress_iface, spoof_gateway_ip)`` 后，会尽力解析经该接口去往该 IP 的**下一跳 lladdr**，
并广播 gratuitous ARP（需安装 **scapy**、与 GARP 相同权限），以减轻下游主机邻居表长期指向本机 MAC 的问题；
可用 ``MTR_OP_ARP_RESTORE_NEIGH=0`` 关闭。

状态文件：``<db 父目录>/.arp_daemon_assigned_host.json``，与守护进程一致，避免重复添加或漏删。

环境变量：

- ``MTR_OP_ARP_ASSIGN_HOST_IP``（OP 进程）：默认 ``1``；``0``/``false``/``no`` 时 OP 不下发 ``ip addr``。
- ``MTR_ARP_ASSIGN_HOST_IP``（守护进程）：默认 ``1``；与守护进程 ``--no-assign-host-ip`` 二选一逻辑一致。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import arp_neighbor_restore

logger = logging.getLogger(__name__)


def assigned_host_state_path(db_path: Path) -> Path:
    return db_path.parent / ".arp_daemon_assigned_host.json"


def _load_desired_pairs_from_state(path: Path) -> Set[Tuple[str, str]]:
    if not path.is_file():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()
    out: Set[Tuple[str, str]] = set()
    if isinstance(raw, dict) and isinstance(raw.get("pairs"), list):
        for item in raw["pairs"]:
            if not isinstance(item, dict):
                continue
            iface = (item.get("iface") or "").strip()
            ip_s = (item.get("ip") or "").strip()
            if iface and ip_s:
                out.add((iface, ip_s))
    return out


def _save_desired_pairs(path: Path, pairs: Set[Tuple[str, str]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"pairs": [{"iface": a, "ip": b} for a, b in sorted(pairs)]}
        path.write_text(json.dumps(data, indent=0), encoding="utf-8")
    except OSError as e:
        logger.warning("arp_spoof_assign: save state %s: %s", path, e)


def iface_has_ipv4(iface: str, want: str) -> bool:
    try:
        p = subprocess.run(
            ["ip", "-j", "addr", "show", "dev", iface],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if p.returncode != 0 or not (p.stdout or "").strip():
            return False
        for row in json.loads(p.stdout):
            for a in row.get("addr_info", []):
                if a.get("family") == "inet" and (a.get("local") or a.get("address") or "").strip() == want:
                    return True
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return False
    return False


def list_ipv4_32_on_iface(iface: str) -> List[str]:
    out: List[str] = []
    try:
        p = subprocess.run(
            ["ip", "-j", "addr", "show", "dev", iface],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if p.returncode != 0 or not (p.stdout or "").strip():
            return out
        for row in json.loads(p.stdout):
            for a in row.get("addr_info", []):
                if a.get("family") != "inet":
                    continue
                if int(a.get("prefixlen", 0)) != 32:
                    continue
                loc = (a.get("local") or a.get("address") or "").strip()
                if loc:
                    out.append(loc)
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return out
    return out


def remove_ipv4_secondary(iface: str, ip_s: str) -> None:
    try:
        ipaddress.ip_address(ip_s)
    except ValueError:
        return
    if ipaddress.ip_address(ip_s).version != 4:
        return
    r = subprocess.run(
        ["ip", "addr", "del", f"{ip_s}/32", "dev", iface],
        capture_output=True,
        text=True,
        timeout=5,
    )
    err = (r.stderr or r.stdout or "").strip().lower()
    if r.returncode != 0:
        if "cannot find" in err or "no such" in err or "not found" in err:
            return
        logger.warning("arp_spoof_assign: del %s/32 dev %s failed: %s", ip_s, iface, err)
    else:
        logger.info("arp_spoof_assign: removed %s/32 dev %s", ip_s, iface)


def ensure_ipv4_secondary(iface: str, ip_s: str) -> bool:
    """在接口上添加 ``ip/32``（若已存在任意前缀的同一地址则跳过）。"""
    try:
        ipaddress.ip_address(ip_s)
    except ValueError:
        return False
    if ipaddress.ip_address(ip_s).version != 4:
        return False
    if iface_has_ipv4(iface, ip_s):
        return True
    r = subprocess.run(
        ["ip", "addr", "add", f"{ip_s}/32", "dev", iface],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        logger.warning("arp_spoof_assign: add %s/32 dev %s failed: %s", ip_s, iface, err)
        return False
    logger.info("arp_spoof_assign: added %s/32 dev %s", ip_s, iface)
    return True


def reconcile_assigned_host_ips(db_path: Path, desired: Set[Tuple[str, str]], verbose_log: bool = False) -> Dict[str, Any]:
    """使接口上由本模块管理的 /32 与 ``desired`` 一致。"""
    path = assigned_host_state_path(db_path)
    stored = _load_desired_pairs_from_state(path)
    ifaces = {iface for iface, _ in stored} | {iface for iface, _ in desired}
    on_wire: Set[Tuple[str, str]] = set()
    for iface in sorted(ifaces):
        for ip_s in list_ipv4_32_on_iface(iface):
            on_wire.add((iface, ip_s))
    removed = 0
    for iface, ip_s in sorted(on_wire - desired):
        remove_ipv4_secondary(iface, ip_s)
        removed += 1
    for iface, ip_s in sorted(desired):
        ensure_ipv4_secondary(iface, ip_s)
    _save_desired_pairs(path, desired)
    # 从「曾保存的 desired」中撤下的网关 IP：发恢复 GARP，减轻下游（如 Linux 201）长期 STALE 错误 lladdr
    for iface, ip_s in sorted(stored - desired):
        try:
            arp_neighbor_restore.restore_after_spoof_removed(iface, ip_s)
        except Exception:
            logger.exception("arp_neighbor_restore failed iface=%s ip=%s", iface, ip_s)
    if verbose_log:
        print(
            f"arp_host_ip: reconcile desired={len(desired)} removed_extra={removed} (see logs for add/del)",
            flush=True,
        )
    return {"removed_pairs": removed, "desired_count": len(desired)}


def load_desired_host_pairs_from_db(db_path: Path) -> Tuple[bool, Set[Tuple[str, str]]]:
    """
    读 OP 库：总开关开且未禁用时返回 ``(True, {(iface, ip), ...})``；
    总开关关返回 ``(False, set())``（应清空本模块管理的 /32）。
    """
    if not db_path.is_file():
        return False, set()
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        row = conn.execute("SELECT arp_spoof_enabled FROM arp_spoof_settings WHERE id = 1").fetchone()
        arp_on = bool(row and int(row[0] or 0))
        if not arp_on:
            return False, set()
        rows = conn.execute(
            "SELECT spoof_gateway_ip, egress_iface, satellite_vrf FROM arp_spoof_targets WHERE enabled = 1 ORDER BY id ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return False, set()
    finally:
        conn.close()
    from . import bgp_ipvlan_reconcile, satellite_vrf_assign

    desired: Set[Tuple[str, str]] = set()
    for r in rows:
        ip_s = (r[0] or "").strip()
        iface = (r[1] or "").strip()
        satellite_vrf = (r[2] or "").strip() if len(r) > 2 else ""
        if not ip_s or not iface:
            continue
        # Satellite BGP rows are owned by either the legacy satellite VRF
        # reconciler or the ipvlan L2 reconciler. Do not also place the same
        # /32 on the physical interface in the main table.
        if satellite_vrf:
            continue
        if satellite_vrf_assign.satellite_spoof_ip_tracked(db_path, ip_s):
            continue
        if bgp_ipvlan_reconcile.satellite_spoof_ip_tracked(db_path, ip_s):
            continue
        desired.add((iface, ip_s))
    return True, desired


def reconcile_from_op_database(db_path: Path, *, assign_enabled: Optional[bool] = None) -> Dict[str, Any]:
    """
    根据 SQLite 当前 ARP 配置执行 reconcile。

    ``assign_enabled``：``None`` 时读环境 ``MTR_OP_ARP_ASSIGN_HOST_IP``（默认开）；
    为 ``False`` 时直接返回 ``skipped``。
    """
    if assign_enabled is None:
        raw = (os.environ.get("MTR_OP_ARP_ASSIGN_HOST_IP") or "1").strip().lower()
        if raw in {"0", "false", "no"}:
            return {"skipped": True, "reason": "MTR_OP_ARP_ASSIGN_HOST_IP off"}
    elif assign_enabled is False:
        return {"skipped": True, "reason": "assign_enabled=False"}

    db_path = Path(db_path).expanduser()
    arp_on, desired = load_desired_host_pairs_from_db(db_path)
    if not arp_on:
        desired = set()
    out = reconcile_assigned_host_ips(db_path, desired, verbose_log=False)
    out["arp_spoof_enabled"] = arp_on
    return out
