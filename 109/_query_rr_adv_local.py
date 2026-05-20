#!/usr/bin/env python3
"""从本机或 SSH 查询 RR 通告明细。"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent
RR = "139.159.43.249"
DS = "139.159.43.208"


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        if name == "env":
            break


def get_json(url: str, timeout: int = 30) -> object:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> None:
    load_env()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109")
    port = os.environ.get("MTR_OP_PORT", "8808")
    op = f"http://{host}:{port}"
    agent = f"http://{host}:9179"

    try:
        neighbors = get_json(f"{op}/api/bgp/neighbors")
    except Exception as e:
        print(f"无法访问 OP {op}: {e}", file=sys.stderr)
        sys.exit(1)

    print("=== RR 邻居行 (gobgp-rr / 249) ===")
    rr_row = None
    for n in neighbors:
        if n.get("vrf") == "gobgp-rr" and n.get("neighbor_ip") == RR:
            rr_row = n
            for k in (
                "session_state",
                "routes_received",
                "routes_sent",
                "advertise_routes",
                "routes_cached",
                "source_ip",
            ):
                print(f"  {k}: {n.get(k)}")
            break
    if not rr_row:
        print("  未找到 RR 行")

    print("\n=== 通告任务状态 ===")
    try:
        st = get_json(f"{op}/api/bgp/neighbors/gobgp-rr/{RR}/advertise/status")
        print(json.dumps(st, ensure_ascii=False, indent=2))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:200]}")

    print("\n=== 聚合来源：下游 (source_ip=249) ===")
    ds_vrf = None
    for n in neighbors:
        if str(n.get("role", "")).lower() == "downstream" and str(n.get("source_ip") or "") == RR:
            print(
                f"  vrf={n.get('vrf')} peer={n.get('neighbor_ip')} "
                f"session={n.get('session_state')} rcvd={n.get('routes_received')} "
                f"cached={n.get('routes_cached')}"
            )
            if n.get("neighbor_ip") == DS:
                ds_vrf = n.get("vrf")

    if not ds_vrf:
        for n in neighbors:
            if n.get("neighbor_ip") == DS and str(n.get("role", "")).lower() == "downstream":
                ds_vrf = n.get("vrf")
                break

    if ds_vrf:
        q = urllib.parse.urlencode(
            {"window": "downstream", "vrf": ds_vrf, "neighbor_ip": DS}
        )
        cnt = get_json(f"{agent}/api/rib/routes/count?{q}")
        print(f"\n=== 下游持久库（发 RR 前读库）vrf={ds_vrf} ===")
        print(f"  count: {cnt.get('count') if isinstance(cnt, dict) else cnt}")
        rib = get_json(
            f"{agent}/api/rib/routes?{q}&page=1&page_size=20", timeout=60
        )
        items = rib.get("items") or rib.get("routes") or []
        total = rib.get("total", len(items))
        print(f"  total: {total}")
        print("  样本（库内 nexthop；发 RR 时已改为 207）:")
        for x in items[:20]:
            print(
                f"    {x.get('prefix', ''):22s} nh={x.get('nexthop', '')} "
                f"as_path={(x.get('as_path') or '')[:40]}"
            )
    else:
        print("  未找到下游 VRF")

    print("\n=== Agent RX 状态 ===")
    try:
        status = get_json(f"{agent}/api/status")
        rx = status.get("rx") or {}
        print(f"  router_id: {rx.get('router_id')}")
        for p in rx.get("rr_peers") or []:
            if p.get("address") == RR:
                print(f"  RR peer: {json.dumps(p, ensure_ascii=False)}")
    except Exception as e:
        print(f"  {e}")

    print("\n说明:")
    print("  routes_received≈1069415 = 从 RR 249 **学到** 的上游路由（ADJ-IN）")
    print("  routes_sent = 向 RR **发出** 的条数（聚合下游库后 RX AddPath）")
    print("  明细在「下游窗」peer RIB；Web 学习路由页选 downstream + 208 可分页查看")


if __name__ == "__main__":
    main()
