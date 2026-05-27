"""bgp-agent 按 peer 百万级 RIB（Redis/RocksDB）OP 侧封装。"""
from __future__ import annotations

import ipaddress
import logging
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import bgp_control, storage

logger = logging.getLogger(__name__)


def _ingest_timeout() -> float:
    raw = (os.environ.get("MTR_BGP_INGEST_TIMEOUT") or "7200").strip()
    try:
        return max(120.0, float(raw))
    except ValueError:
        return 7200.0


def peer_route_window(role: str) -> str:
    return storage.route_window_for_bgp_role(role)


def sync_peer_rib_policy(
    vrf: str,
    neighbor_ip: str,
    role: str,
    store_received_routes: int,
    source_ip: str = "",
    enabled: Optional[bool] = None,
) -> None:
    """将 SQLite meta 中的邻居策略下发到 Agent Redis（peer:policy:*）。"""
    bgp_control.require_agent()
    window = peer_route_window(role)
    en = bool(store_received_routes) if enabled is None else bool(enabled)
    sr = en if enabled is not None else bool(store_received_routes)
    sip = (source_ip or "").strip()
    if window == "downstream" and sip:
        sip = storage.validate_ipv4(sip)
    participate_fib = True
    if window == "upstream":
        participate_fib = True
    elif en is False:
        participate_fib = False
    body = {
        "vrf": storage.validate_vrf_name(vrf),
        "neighbor_ip": storage.validate_ipv4(neighbor_ip),
        "window": window,
        "store_routes": sr,
        "enabled": en,
        "source_ip": sip,
        "participate_fib": participate_fib,
    }
    with bgp_control._client() as c:
        r = c.post("/api/rib/policy", json=body, timeout=30.0)
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")


def sync_peer_policy_from_meta(
    conn,
    vrf: str,
    neighbor_ip: str,
    *,
    enabled: Optional[bool] = None,
) -> None:
    """按 SQLite bgp_neighbor_meta 单行同步 Agent policy（以库为准）。"""
    v = storage.validate_vrf_name(vrf)
    ip = storage.validate_ipv4(neighbor_ip)
    meta = storage.get_bgp_neighbor_meta_map(conn, v).get(ip)
    role = (meta[0] if meta else "unknown") or "unknown"
    sip = (meta[2] if meta and len(meta) > 2 else "") or ""
    if not sip:
        try:
            row = next(
                (
                    r
                    for r in bgp_control.list_agent_neighbors()
                    if storage.validate_vrf_name(str(r.get("vrf") or "")) == v
                    and str(r.get("address") or "").strip() == ip
                ),
                None,
            )
            if row:
                sip = str(row.get("local_address") or "").strip()
                if enabled is None:
                    enabled = bool(row.get("enabled", True))
        except Exception:
            pass
    sr = storage.get_bgp_neighbor_store_received_routes(conn, v, ip)
    if sr == 0 and (enabled is None or enabled):
        sr = 1
    sync_peer_rib_policy(v, ip, role, sr, sip, enabled if enabled is not None else bool(sr))


def sync_all_peer_policies_from_sqlite(conn) -> Dict[str, Any]:
    """部署/恢复：把 SQLite 全部邻居 meta 同步到 Agent Redis policy。"""
    synced: List[str] = []
    errors: List[str] = []
    try:
        bgp_control.require_agent()
    except Exception as e:
        return {"synced": synced, "errors": [str(e)]}
    agent_by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
    try:
        for row in bgp_control.list_agent_neighbors():
            v = storage.validate_vrf_name(str(row.get("vrf") or "default"))
            addr = str(row.get("address") or "").strip()
            if not addr:
                continue
            try:
                ip = storage.validate_ipv4(addr)
            except ValueError:
                continue
            agent_by_key[(v, ip)] = row
    except Exception as e:
        logger.warning("sync policies: list agent neighbors: %s", e)
    for vrf, nip, role, _note, src in bgp_control.iter_bgp_neighbor_meta(conn):
        try:
            v = storage.validate_vrf_name(vrf)
            ip = storage.validate_ipv4(nip)
            row = agent_by_key.get((v, ip), {})
            enabled = bool(row.get("enabled", True))
            sr = storage.get_bgp_neighbor_store_received_routes(conn, v, ip)
            if sr == 0 and enabled:
                sr = 1
            sip = (src or "").strip() or str(row.get("local_address") or "").strip()
            sync_peer_rib_policy(v, ip, role, sr, sip, enabled)
            synced.append(f"{v}:{ip}")
        except Exception as e:
            errors.append(f"{vrf}:{nip}:{e}")
            logger.warning("sync policy %s/%s: %s", vrf, nip, e)
    return {"synced": synced, "errors": errors}


def _rib_query_params(
    vrf: str,
    neighbor_ip: str,
    role: str,
    source_ip: str = "",
) -> Dict[str, str]:
    window = peer_route_window(role)
    params: Dict[str, str] = {
        "window": window,
        "vrf": storage.validate_vrf_name(vrf),
        "neighbor_ip": storage.validate_ipv4(neighbor_ip),
    }
    sip = (source_ip or "").strip()
    if window == "downstream" and sip:
        params["source_ip"] = storage.validate_ipv4(sip)
    return params


def count_peer_rib_routes(
    vrf: str,
    neighbor_ip: str,
    role: str,
    source_ip: str = "",
) -> int:
    bgp_control.require_agent()
    with bgp_control._client() as c:
        r = c.get(
            "/api/rib/routes/count",
            params=_rib_query_params(vrf, neighbor_ip, role, source_ip),
            timeout=60.0,
        )
        r.raise_for_status()
        return int((r.json() or {}).get("count") or 0)


def _prefix_exact_equal(a: str, b: str) -> bool:
    try:
        return ipaddress.ip_network(a.strip(), strict=False) == ipaddress.ip_network(
            b.strip(), strict=False
        )
    except ValueError:
        return a.strip() == b.strip()


def _scan_peer_rib_route_exact(
    vrf: str,
    neighbor_ip: str,
    role: str,
    prefix: str,
) -> Optional[Dict[str, Any]]:
    """旧版 Agent 无 prefix 参数时，分页扫描直至命中或扫完。"""
    pfx = prefix.strip()
    if not pfx:
        return None
    max_pages = int(os.environ.get("MTR_BGP_PREFIX_SCAN_MAX_PAGES", "0") or "0")
    page_size = min(5000, max(500, int(os.environ.get("MTR_BGP_PREFIX_SCAN_PAGE_SIZE", "2000"))))
    page = 1
    while True:
        data = list_peer_rib_routes_page(vrf, neighbor_ip, role, page=page, page_size=page_size)
        for item in data.get("routes") or []:
            if _prefix_exact_equal(str(item.get("prefix") or ""), pfx):
                return dict(item)
        total = int(data.get("total") or 0)
        if page * page_size >= total:
            break
        if max_pages > 0 and page >= max_pages:
            logger.warning(
                "prefix scan truncated vrf=%s neighbor=%s prefix=%s pages=%s",
                vrf,
                neighbor_ip,
                pfx,
                page,
            )
            break
        page += 1
    return None


def get_peer_rib_route(
    vrf: str,
    neighbor_ip: str,
    role: str,
    prefix: str,
    source_ip: str = "",
) -> Optional[Dict[str, Any]]:
    """按前缀精确查询；新 Agent 走 Redis O(1)，旧 Agent 回退分页扫描。"""
    bgp_control.require_agent()
    window = peer_route_window(role)
    pfx = prefix.strip()
    if not pfx:
        return None
    params = _rib_query_params(vrf, neighbor_ip, role, source_ip)
    params["prefix"] = pfx
    with bgp_control._client() as c:
        r = c.get(
            "/api/rib/routes",
            params=params,
            timeout=120.0,
        )
        r.raise_for_status()
        data = r.json() or {}
    routes = list(data.get("routes") or [])
    resp_pfx = (data.get("prefix") or "").strip()
    # 新 Agent 会在响应里回显规范化后的 prefix
    if resp_pfx and _prefix_exact_equal(resp_pfx, pfx):
        for item in routes:
            if _prefix_exact_equal(str(item.get("prefix") or ""), pfx):
                return dict(item)
        return None
    # 旧 Agent 忽略 prefix，勿误用分页首条
    for item in routes:
        if _prefix_exact_equal(str(item.get("prefix") or ""), pfx):
            return dict(item)
    return _scan_peer_rib_route_exact(vrf, neighbor_ip, role, pfx)


def list_peer_rib_routes_page(
    vrf: str,
    neighbor_ip: str,
    role: str,
    page: int = 1,
    page_size: int = 100,
    source_ip: str = "",
) -> Dict[str, Any]:
    bgp_control.require_agent()
    params = _rib_query_params(vrf, neighbor_ip, role, source_ip)
    params["page"] = str(max(1, page))
    params["page_size"] = str(min(5000, max(1, page_size)))
    with bgp_control._client() as c:
        r = c.get(
            "/api/rib/routes",
            params=params,
            timeout=120.0,
        )
        r.raise_for_status()
        return r.json() or {}


def list_peer_rib_routes_slice(
    vrf: str,
    neighbor_ip: str,
    role: str,
    start_offset: int,
    limit: int,
    source_ip: str = "",
) -> List[Dict[str, Any]]:
    """从某 peer 持久库指定偏移起读取最多 limit 条。"""
    if limit <= 0 or start_offset < 0:
        return []
    chunk_size = min(5000, max(limit, 200))
    page = start_offset // chunk_size + 1
    skip = start_offset % chunk_size
    out: List[Dict[str, Any]] = []
    while len(out) < limit:
        data = list_peer_rib_routes_page(
            vrf, neighbor_ip, role, page=page, page_size=chunk_size, source_ip=source_ip
        )
        routes = list(data.get("routes") or [])
        if skip:
            routes = routes[skip:]
            skip = 0
        if not routes:
            break
        out.extend(routes[: limit - len(out)])
        total = int(data.get("total") or 0)
        if page * chunk_size >= total:
            break
        page += 1
    return out


def list_merged_rib_routes_page(
    peers: List[Tuple[str, str, str, str]],
    page: int = 1,
    page_size: int = 100,
) -> Dict[str, Any]:
    """多 peer 合并分页（peers: vrf, neighbor_ip, role, source_ip）。"""
    page = max(1, page)
    page_size = min(5000, max(1, page_size))
    counts: List[int] = []
    total = 0
    for vrf, nip, role, sip in peers:
        try:
            c = count_peer_rib_routes(vrf, nip, role, sip)
        except Exception:
            c = 0
        counts.append(c)
        total += c

    global_offset = (page - 1) * page_size
    remaining_skip = global_offset
    merged: List[Dict[str, Any]] = []

    for (vrf, nip, role, sip), cnt in zip(peers, counts):
        if len(merged) >= page_size:
            break
        if remaining_skip >= cnt:
            remaining_skip -= cnt
            continue
        peer_offset = remaining_skip
        remaining_skip = 0
        need = page_size - len(merged)
        chunk = list_peer_rib_routes_slice(vrf, nip, role, peer_offset, need, sip)
        rw = peer_route_window(role)
        for item in chunk:
            merged.append(
                {
                    **item,
                    "vrf": str(item.get("vrf") or vrf),
                    "neighbor_ip": str(item.get("neighbor_ip") or nip),
                    "window": str(item.get("window") or rw),
                }
            )

    return {
        "routes": merged,
        "total": total,
        "page": page,
        "page_size": page_size,
        "data_store": "redis+rocksdb",
    }


def iter_peer_rib_routes(
    vrf: str,
    neighbor_ip: str,
    role: str,
    page_size: int = 5000,
) -> Iterator[Tuple[str, str]]:
    page = 1
    while True:
        data = list_peer_rib_routes_page(vrf, neighbor_ip, role, page=page, page_size=page_size)
        routes = data.get("routes") or []
        if not routes:
            break
        for item in routes:
            pfx = str(item.get("prefix") or "").strip()
            if pfx:
                yield pfx, str(item.get("nexthop") or "").strip()
        total = int(data.get("total") or 0)
        if page * page_size >= total:
            break
        page += 1


def ingest_peer_routes(vrf: str, neighbor_ip: str, role: str) -> Dict[str, Any]:
    """将对端 IP 在 ADJ-IN 中已通告的路由全量灌入按 peer 的持久库（上游/下游统一）。"""
    bgp_control.require_agent()
    window = peer_route_window(role)
    with bgp_control._client() as c:
        r = c.post(
            "/api/rib/ingest-peer",
            params={
                "window": window,
                "vrf": storage.validate_vrf_name(vrf),
                "neighbor_ip": storage.validate_ipv4(neighbor_ip),
            },
            timeout=_ingest_timeout(),
        )
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
        return r.json() or {}


def ingest_downstream_peer_routes(vrf: str, neighbor_ip: str) -> Dict[str, Any]:
    """兼容旧名。"""
    return ingest_peer_routes(vrf, neighbor_ip, "downstream")


def ensure_peer_enabled_policy(
    vrf: str,
    neighbor_ip: str,
    role: str,
    source_ip: str = "",
) -> None:
    """enabled 邻居默认入库（目标架构：无 store_routes 开关）。"""
    sync_peer_rib_policy(vrf, neighbor_ip, role, 1, source_ip, True)


def purge_peer_rib(
    vrf: str,
    neighbor_ip: str,
    role: str,
    source_ip: str = "",
) -> Dict[str, Any]:
    bgp_control.require_agent()
    window = peer_route_window(role)
    params: Dict[str, str] = {
        "window": window,
        "vrf": storage.validate_vrf_name(vrf),
        "neighbor_ip": storage.validate_ipv4(neighbor_ip),
    }
    if source_ip.strip():
        params["source_ip"] = storage.validate_ipv4(source_ip.strip())
    with bgp_control._client() as c:
        r = c.post("/api/rib/purge-peer", params=params, timeout=600.0)
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
        return r.json() or {}


def export_reconcile() -> Dict[str, Any]:
    """Agent 启动后 FIB diff reconcile（替代全量 advertise 扫库）。"""
    bgp_control.require_agent()
    with bgp_control._client() as c:
        r = c.post("/api/export/reconcile", timeout=30.0)
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
        return r.json() or {}


def pipeline_consistency() -> Dict[str, Any]:
    bgp_control.require_agent()
    with bgp_control._client() as c:
        r = c.get("/api/pipeline/consistency", timeout=30.0)
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
        return r.json() or {}


def pipeline_repair(window: str) -> Dict[str, Any]:
    bgp_control.require_agent()
    w = (window or "upstream").strip()
    with bgp_control._client() as c:
        r = c.post(f"/api/pipeline/repair?window={w}", timeout=30.0)
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
        return r.json() or {}


def pipeline_job_status(job_id: str) -> Dict[str, Any]:
    bgp_control.require_agent()
    jid = (job_id or "").strip()
    with bgp_control._client() as c:
        r = c.get(f"/api/pipeline/status?job_id={jid}", timeout=30.0)
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
        return r.json() or {}


def ingest_peer_routes_with_source(
    vrf: str,
    neighbor_ip: str,
    role: str,
    source_ip: str = "",
) -> Dict[str, Any]:
    bgp_control.require_agent()
    window = peer_route_window(role)
    params: Dict[str, str] = {
        "window": window,
        "vrf": storage.validate_vrf_name(vrf),
        "neighbor_ip": storage.validate_ipv4(neighbor_ip),
    }
    if source_ip.strip():
        params["source_ip"] = storage.validate_ipv4(source_ip.strip())
    with bgp_control._client() as c:
        r = c.post("/api/rib/ingest-peer", params=params, timeout=_ingest_timeout())
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
        return r.json() or {}
