"""双向学路由：定时从 bgp-agent 拉 RR(RX) 与下游(TX ADJ-IN) 写入 SQLite；断链 freeze 保留快照。"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from . import bgp_control, bgp_learned_routes_sync, bgp_sticky_reconcile, storage

logger = logging.getLogger(__name__)


def _ts() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _peer_established(state: str) -> bool:
    return "ESTABLISHED" in (state or "").upper()


def sync_downstream_routes_for_vrf(conn, vrf: str, ts: str) -> int:
    """从 TX ADJ-IN 拉取该卫星 VRF 下各邻居路由；断链 peer 跳过覆盖（freeze 保留库内）。"""
    vrf_n = storage.validate_vrf_name(vrf)
    if vrf_n in (bgp_control.GOBGP_VRF_RR, "default"):
        return 0
    try:
        routes = bgp_control.list_tx_learned_routes(vrf_n)
    except Exception as e:
        logger.warning("tx learned routes vrf=%s: %s", vrf_n, e)
        return 0
    by_neighbor: Dict[str, List[tuple]] = {}
    for item in routes:
        if not isinstance(item, dict):
            continue
        nip = str(item.get("neighbor") or item.get("neighbor_ip") or "").strip()
        pfx = str(item.get("prefix") or "").strip()
        if not nip or not pfx:
            continue
        nh = str(item.get("nexthop") or "").strip()
        asp = str(item.get("as_path") or "")[:512]
        ras = int(item.get("remote_as") or bgp_control.default_local_as())
        by_neighbor.setdefault(nip, []).append(
            (pfx, nh, nip, ras, "downstream", asp, ts, "downstream")
        )
    total = 0
    for nip, rows in by_neighbor.items():
        frozen = storage.is_bgp_peer_frozen(conn, vrf_n, nip)
        if frozen:
            logger.info("downstream sync skip frozen peer %s/%s (%s routes in agent)", vrf_n, nip, len(rows))
            storage.touch_bgp_peer_snapshot(conn, vrf_n, nip, "downstream", route_count=storage.count_routes_for_peer(conn, vrf_n, nip))
            continue
        if not _peer_established(bgp_control.neighbor_session_state(vrf_n, nip)):
            storage.set_bgp_peer_frozen(conn, vrf_n, nip, "downstream", True)
            logger.info("downstream peer not established, freeze snapshot %s/%s", vrf_n, nip)
            continue
        storage.set_bgp_peer_frozen(conn, vrf_n, nip, "downstream", False)
        storage.replace_bgp_learned_routes_for_peer(conn, vrf_n, nip, rows)
        storage.touch_bgp_peer_snapshot(conn, vrf_n, nip, "downstream", route_count=len(rows))
        total += len(rows)
    return total


def sync_bidirectional_routes(db_path: Path) -> Dict[str, Any]:
    """RR 方向沿用原 sync + 各卫星 VRF 下游 ADJ-IN；并刷新 peer freeze 状态。"""
    summary: Dict[str, Any] = {"upstream": {}, "downstream": {}, "freeze": {}}
    sticky = bgp_learned_routes_sync.sync_bgp_learned_routes(db_path)
    summary["upstream"] = {"sticky": sticky}
    conn = storage.connect(db_path)
    try:
        storage.init_schema(conn)
        ts = _ts()
        if not bgp_control.health_ok():
            return {"error": "bgp_agent_unavailable"}
        try:
            freeze_status = bgp_control.get_peers_freeze_status()
            summary["freeze"] = freeze_status
        except Exception:
            logger.exception("peers freeze status")
        ds_total = 0
        for row in bgp_control.list_agent_neighbors():
            vrf = storage.validate_vrf_name(str(row.get("vrf") or "default"))
            if vrf == bgp_control.GOBGP_VRF_RR:
                continue
            ds_total += sync_downstream_routes_for_vrf(conn, vrf, ts)
        summary["downstream"] = {"routes_synced": ds_total}
        storage.set_bgp_rib_sync_state(conn, ts, True, "")
        logger.info("bidirectional sync done downstream_routes=%s", ds_total)
    except Exception as e:
        logger.exception("sync_bidirectional_routes")
        try:
            storage.set_bgp_rib_sync_state(conn, _ts(), False, str(e))
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return summary
