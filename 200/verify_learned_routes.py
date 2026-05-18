#!/usr/bin/env python3
"""验收 BGP 路由页 API：filter-options + 分页明细。"""
from __future__ import annotations

import json
import sys
import urllib.request

HOST = "10.133.151.200"
PORT = 8808
RR_VRF = "gobgp-rr"
RR_IP = "10.133.153.204"


def get(path: str) -> dict:
    with urllib.request.urlopen(f"http://{HOST}:{PORT}{path}", timeout=60) as r:
        return json.loads(r.read().decode())


def main() -> int:
    opts = get("/api/bgp/learned-routes/filter-options")
    summary = opts.get("summary") or {}
    print("filter-options summary:", summary)
    pairs = opts.get("peer_pairs") or []
    rr_pair = [p for p in pairs if p.get("vrf") == RR_VRF and p.get("neighbor_ip") == RR_IP]
    if not rr_pair:
        print(f"WARN: peer_pairs 无 {RR_VRF}/{RR_IP}")
    q = (
        f"/api/bgp/learned-routes?vrf={RR_VRF}&neighbor_ip={RR_IP}"
        "&route_window=upstream&page=1&page_size=5"
    )
    data = get(q)
    total = int(data.get("total") or 0)
    routes = data.get("routes") or []
    print(f"list total={total} page_routes={len(routes)}")
    if routes:
        print("sample:", routes[0].get("prefix"), routes[0].get("nexthop"))
    if total < 1:
        print("FAIL: 无入库路由")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
