#!/usr/bin/env python3
"""BGP FIB 百万级压测辅助：Agent FIB count/分页/reconcile 基线（实验室用）。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request


def agent_url() -> str:
    return (os.environ.get("GOBGP_AGENT_URL") or "http://127.0.0.1:9179").rstrip("/")


def http_json(method: str, path: str, params: dict | None = None) -> dict:
    url = agent_url() + path
    if params:
        q = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url += "?" + q
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode() or "{}")


def bench_fib_count(window: str) -> int:
    r = http_json("GET", "/api/fib/routes/count", {"window": window})
    return int(r.get("count") or 0)


def bench_fib_page(window: str, page: int, page_size: int) -> float:
    t0 = time.perf_counter()
    http_json("GET", "/api/fib/routes", {"window": window, "page": page, "page_size": page_size})
    return time.perf_counter() - t0


def main() -> int:
    p = argparse.ArgumentParser(description="BGP Agent FIB 压测基线")
    p.add_argument("--window", default="upstream", choices=["upstream", "downstream"])
    p.add_argument("--count-only", action="store_true")
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--recompute", action="store_true")
    p.add_argument("--reconcile", action="store_true")
    args = p.parse_args()

    if args.recompute:
        http_json("POST", "/api/fib/recompute", {"window": args.window})
        print("recompute triggered")
    if args.reconcile:
        http_json("POST", "/api/export/reconcile")
        print("export reconcile triggered")

    cnt = bench_fib_count(args.window)
    print(f"fib:{args.window} count={cnt}")
    if args.count_only:
        return 0

    elapsed = bench_fib_page(args.window, 1, args.page_size)
    print(f"fib page page_size={args.page_size} elapsed={elapsed:.3f}s")
    return 0 if elapsed < 2.0 else 1


if __name__ == "__main__":
    sys.exit(main())
