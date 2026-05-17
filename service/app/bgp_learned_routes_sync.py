"""从 GoBGP Agent（RX 有效 RIB）拉取路由写入 SQLite；上游前缀缓存供 sticky 下游通告。"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from . import bgp_control, bgp_sticky_reconcile, storage
from .bgp_route_cache import get_global_cache

logger = logging.getLogger(__name__)


def first_asn_from_path(path: str) -> int:
    for tok in (path or "").replace("{", " ").replace("}", " ").replace(",", " ").split():
        t = tok.strip()
        if t.isdigit():
            v = int(t)
            if 1 <= v <= 4294967295:
                return v
    return 0


def _role_for_route(
    conn,
    vrf: str,
    neighbor_ip: str,
    nh: str,
    env_rr: str,
) -> str:
    meta = storage.get_bgp_neighbor_meta_map(conn, vrf)
    if neighbor_ip and neighbor_ip in meta:
        r = str(meta[neighbor_ip][0] or "unknown").strip().lower()
        if r in storage.BGP_META_ROLES:
            return r
    if neighbor_ip == env_rr or nh == env_rr:
        return "rr"
    gl_peer, gl_role = storage.lookup_bgp_neighbor_meta_for_nexthop(conn, vrf, nh) if nh else ("", "unknown")
    if gl_peer and gl_peer in meta:
        r = str(meta[gl_peer][0] or "unknown").strip().lower()
        if r in storage.BGP_META_ROLES:
            return r
    gr = str(gl_role or "unknown").strip().lower()
    if gr in storage.BGP_META_ROLES:
        return gr
    return "unknown"


def sync_bgp_learned_routes(db_path: Path, use_threading: bool = True) -> Dict[str, Any]:
    """从 bgp-agent 同步学习路由；``use_threading`` 保留参数兼容，实际单路径。"""
    del use_threading
    conn = storage.connect(db_path)
    sticky_summary: Dict[str, Any] = {}
    try:
        storage.init_schema(conn)
        ts = datetime.utcnow().isoformat() + "Z"
        if not bgp_control.health_ok():
            storage.set_bgp_rib_sync_state(conn, ts, False, "bgp_agent_unavailable")
            return {"error": "bgp_agent_unavailable"}

        env = bgp_control.agent_env_config()
        rr_ip = env["rr_addr"]
        rr_vrf = bgp_control.GOBGP_VRF_RR
        cache_vrf = bgp_sticky_reconcile.upstream_cache_learn_vrf()

        try:
            routes = bgp_control.list_agent_routes()
        except Exception as e:
            storage.set_bgp_rib_sync_state(conn, ts, False, str(e))
            raise

        active_vrfs: Set[str] = {rr_vrf}
        by_vrf: Dict[str, List[tuple]] = {rr_vrf: []}
        upstream_ips: Set[str] = set()

        for nip, tup in storage.get_bgp_neighbor_meta_map(conn, cache_vrf).items():
            if str((tup[0] if tup else "") or "").strip().lower() in {"upstream", "rr"}:
                upstream_ips.add(nip)

        for item in routes:
            if not isinstance(item, dict):
                continue
            pfx = str(item.get("prefix") or item.get("Prefix") or "").strip()
            if not pfx:
                continue
            nh = str(item.get("nexthop") or item.get("Nexthop") or rr_ip).strip()
            asp = str(item.get("as_path") or item.get("ASPath") or "")[:512]
            ras = int(item.get("remote_as") or item.get("RemoteAS") or env["rr_as"])
            peer = rr_ip
            role = _role_for_route(conn, rr_vrf, peer, nh, rr_ip)
            if role == "upstream" or nh in upstream_ips or peer in upstream_ips:
                upstream_ips.add(peer)
            by_vrf[rr_vrf].append((pfx, nh, peer, ras, role, asp, ts, "upstream"))

        if cache_vrf != rr_vrf:
            active_vrfs.add(cache_vrf)
            by_vrf.setdefault(cache_vrf, [])

        for row in bgp_control.list_agent_neighbors():
            v = storage.validate_vrf_name(str(row.get("vrf") or "default"))
            if v == bgp_control.GOBGP_VRF_RR:
                continue
            active_vrfs.add(v)

        env_rr = env["rr_addr"]
        rr_est = bgp_control.rr_is_established()
        if env_rr:
            storage.set_bgp_peer_frozen(conn, rr_vrf, env_rr, "upstream", not rr_est)
            storage.touch_bgp_peer_snapshot(
                conn,
                rr_vrf,
                env_rr,
                "upstream",
                route_count=len(by_vrf.get(rr_vrf, [])),
                session_established=rr_est,
            )

        for vrf, rows in by_vrf.items():
            do_replace = True
            if vrf == rr_vrf and env_rr and not rr_est:
                do_replace = False
                logger.info(
                    "bgp learn vrf=%s RR not Established — keep SQLite snapshot (freeze)",
                    vrf,
                )
            if not rows and vrf == cache_vrf:
                up_ip = bgp_sticky_reconcile.first_upstream_neighbor_ip(conn, vrf)
                if up_ip and not bgp_control.neighbor_is_established(vrf, up_ip):
                    do_replace = False
                    logger.info(
                        "bgp learn vrf=%s empty, upstream %s not Established — keep SQLite",
                        vrf,
                        up_ip,
                    )
                elif not up_ip and storage.list_bgp_upstream_cache_rows(conn, vrf):
                    do_replace = False
            if do_replace:
                storage.replace_bgp_learned_routes_for_vrf(conn, vrf, rows)
                try:
                    get_global_cache().update_routes(vrf, rows)
                except Exception:
                    logger.exception("route cache update")

            if vrf == cache_vrf and rows:
                cache_rows: List[tuple] = []
                for tup in rows:
                    if len(tup) < 7:
                        continue
                    role_t = str(tup[4] or "").strip().lower()
                    nh, peer = str(tup[1] or "").strip(), str(tup[2] or "").strip()
                    if role_t in {"upstream", "rr"} or nh in upstream_ips or peer in upstream_ips:
                        cache_rows.append(
                            (vrf, tup[0], nh, peer, int(tup[3]), tup[5], ts)
                        )
                if cache_rows:
                    storage.bulk_upsert_bgp_upstream_route_cache(conn, cache_rows)

        storage.delete_bgp_learned_routes_not_in_vrfs(conn, active_vrfs)
        try:
            bgp_sticky_reconcile.maybe_prune_upstream_cache(conn, cache_vrf)
        except Exception:
            logger.exception("bgp upstream cache prune")
        try:
            sticky_summary = bgp_sticky_reconcile.reconcile_sticky_for_downstream(conn)
        except Exception:
            logger.exception("bgp sticky reconcile")
        storage.set_bgp_rib_sync_state(conn, ts, True, "")
        logger.info("gobgp learned routes sync: %s prefixes", sum(len(v) for v in by_vrf.values()))
    except Exception as e:
        logger.exception("sync_bgp_learned_routes")
        try:
            storage.set_bgp_rib_sync_state(
                conn, datetime.utcnow().isoformat() + "Z", False, str(e)
            )
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return sticky_summary
