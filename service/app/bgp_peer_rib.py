"""bgp-agent 按 peer 百万级 RIB（Redis/RocksDB）OP 侧封装。"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import bgp_control, storage


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
) -> None:
    bgp_control.require_agent()
    window = peer_route_window(role)
    body = {
        "vrf": storage.validate_vrf_name(vrf),
        "neighbor_ip": storage.validate_ipv4(neighbor_ip),
        "window": window,
        "store_routes": bool(store_received_routes),
    }
    with bgp_control._client() as c:
        r = c.post("/api/rib/policy", json=body, timeout=30.0)
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")


def count_peer_rib_routes(vrf: str, neighbor_ip: str, role: str) -> int:
    bgp_control.require_agent()
    window = peer_route_window(role)
    with bgp_control._client() as c:
        r = c.get(
            "/api/rib/routes/count",
            params={
                "window": window,
                "vrf": storage.validate_vrf_name(vrf),
                "neighbor_ip": storage.validate_ipv4(neighbor_ip),
            },
            timeout=60.0,
        )
        r.raise_for_status()
        return int((r.json() or {}).get("count") or 0)


def list_peer_rib_routes_page(
    vrf: str,
    neighbor_ip: str,
    role: str,
    page: int = 1,
    page_size: int = 100,
) -> Dict[str, Any]:
    bgp_control.require_agent()
    window = peer_route_window(role)
    with bgp_control._client() as c:
        r = c.get(
            "/api/rib/routes",
            params={
                "window": window,
                "vrf": storage.validate_vrf_name(vrf),
                "neighbor_ip": storage.validate_ipv4(neighbor_ip),
                "page": max(1, page),
                "page_size": min(5000, max(1, page_size)),
            },
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
            vrf, neighbor_ip, role, page=page, page_size=chunk_size
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
    peers: List[Tuple[str, str, str]],
    page: int = 1,
    page_size: int = 100,
) -> Dict[str, Any]:
    """多 peer 合并分页（peers 建议已排序）。"""
    page = max(1, page)
    page_size = min(5000, max(1, page_size))
    counts: List[int] = []
    total = 0
    for vrf, nip, role in peers:
        try:
            c = count_peer_rib_routes(vrf, nip, role)
        except Exception:
            c = 0
        counts.append(c)
        total += c

    global_offset = (page - 1) * page_size
    remaining_skip = global_offset
    merged: List[Dict[str, Any]] = []

    for (vrf, nip, role), cnt in zip(peers, counts):
        if len(merged) >= page_size:
            break
        if remaining_skip >= cnt:
            remaining_skip -= cnt
            continue
        peer_offset = remaining_skip
        remaining_skip = 0
        need = page_size - len(merged)
        chunk = list_peer_rib_routes_slice(vrf, nip, role, peer_offset, need)
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


def start_rib_advertise_job(
    task_id: str,
    src_window: str,
    src_vrf: str,
    src_neighbor_ip: str,
    *,
    target: str,
    target_vrf: str = "",
    enable: bool = True,
    batch_size: int = 5000,
    src_peers: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """启动 Agent 流式通告/撤销任务（IteratePeerRoutes，非分页 HTTP）。"""
    bgp_control.require_agent()
    body: Dict[str, Any] = {
        "task_id": task_id,
        "target": target,
        "target_vrf": storage.validate_vrf_name(target_vrf) if target_vrf else "",
        "enable": bool(enable),
        "batch_size": batch_size,
    }
    if src_peers:
        body["src_peers"] = [
            {
                "window": str(p.get("window") or "downstream"),
                "vrf": storage.validate_vrf_name(str(p["vrf"])),
                "neighbor_ip": storage.validate_ipv4(str(p["neighbor_ip"])),
            }
            for p in src_peers
        ]
    else:
        body["src_window"] = src_window
        body["src_vrf"] = storage.validate_vrf_name(src_vrf)
        body["src_neighbor_ip"] = storage.validate_ipv4(src_neighbor_ip)
    path = "/api/rib/advertise" if enable else "/api/rib/withdraw"
    with bgp_control._client() as c:
        r = c.post(path, json=body, timeout=60.0)
        if r.status_code == 409:
            raise RuntimeError("rib_advertise_task_running")
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
        return r.json() or {}


def get_rib_advertise_status(task_id: str) -> Dict[str, Any]:
    bgp_control.require_agent()
    with bgp_control._client() as c:
        r = c.get(
            "/api/rib/advertise/status",
            params={"task_id": task_id},
            timeout=30.0,
        )
        if r.status_code == 404:
            return {"status": "idle", "task_id": task_id}
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
        return r.json() or {}
