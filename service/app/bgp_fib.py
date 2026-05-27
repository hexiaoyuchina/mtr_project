"""bgp-agent FIB（合并转发表）OP 侧封装。"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from . import bgp_control

logger = logging.getLogger(__name__)


def _window_or_upstream(route_window: Optional[str]) -> str:
    w = (route_window or "").strip().lower()
    if w in {"upstream", "downstream"}:
        return w
    return "upstream"


def count_fib_routes(route_window: str) -> int:
    bgp_control.require_agent()
    window = _window_or_upstream(route_window)
    with bgp_control._client() as c:
        r = c.get("/api/fib/routes/count", params={"window": window}, timeout=60.0)
        r.raise_for_status()
        return int((r.json() or {}).get("count") or 0)


def fib_summary() -> Dict[str, int]:
    up = count_fib_routes("upstream")
    dn = count_fib_routes("downstream")
    return {"upstream": up, "downstream": dn, "total": up + dn}


def list_fib_routes_page(
    route_window: str,
    page: int = 1,
    page_size: int = 100,
) -> Dict[str, Any]:
    bgp_control.require_agent()
    window = _window_or_upstream(route_window)
    page = max(1, int(page))
    page_size = max(1, min(1000, int(page_size)))
    with bgp_control._client() as c:
        r = c.get(
            "/api/fib/routes",
            params={"window": window, "page": page, "page_size": page_size},
            timeout=120.0,
        )
        r.raise_for_status()
        data = dict(r.json() or {})
    data["route_window"] = window
    return data


def get_fib_route(route_window: str, prefix: str) -> Optional[Dict[str, Any]]:
    bgp_control.require_agent()
    window = _window_or_upstream(route_window)
    pfx = (prefix or "").strip()
    if not pfx:
        return None
    with bgp_control._client() as c:
        r = c.get(
            "/api/fib/routes",
            params={"window": window, "prefix": pfx},
            timeout=60.0,
        )
        r.raise_for_status()
        data = r.json() or {}
    routes = list(data.get("routes") or [])
    resp_pfx = (data.get("prefix") or "").strip()
    if resp_pfx:
        for item in routes:
            if str(item.get("prefix") or "").strip() == resp_pfx:
                return dict(item)
        return None
    for item in routes:
        if str(item.get("prefix") or "").strip() == pfx:
            return dict(item)
    return None
