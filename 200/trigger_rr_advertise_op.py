#!/usr/bin/env python3
"""经 OP 重触发 gobgp-rr 聚合通告（advertise_routes=1）。"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

LAB = Path(__file__).resolve().parent
RR_VRF, RR_IP = "gobgp-rr", "10.133.153.204"


def load_env() -> str:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    return f"http://{host}:8808"


def req(method: str, url: str, body: dict | None = None, timeout: int = 60):
    data = json.dumps(body).encode() if body else None
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw[:500]}


def main() -> int:
    base = load_env()
    path = f"{base}/api/bgp/neighbors/{RR_VRF}/{RR_IP}/advertise"
    print("GET neighbors (rr row)…")
    code, neighbors = req("GET", f"{base}/api/bgp/neighbors", timeout=180)
    rr = next((n for n in (neighbors if isinstance(neighbors, list) else []) if n.get("neighbor_ip") == RR_IP), {})
    print(
        "before:",
        json.dumps(
            {k: rr.get(k) for k in ("session_state", "routes_received", "routes_sent", "routes_cached", "advertise_routes")},
            ensure_ascii=False,
        ),
    )
    print("POST advertise enable…")
    code, j = req("POST", path, {"advertise_routes": 1}, timeout=120)
    print("advertise http", code, json.dumps(j, ensure_ascii=False)[:400])
    task_id = f"{RR_VRF}-{RR_IP}-advertise"
    for i in range(240):
        time.sleep(5)
        code, st = req("GET", f"{base}/api/bgp/neighbors/{RR_VRF}/{RR_IP}/advertise/status", timeout=30)
        if code != 200:
            print(i, "status http", code)
            continue
        status = st.get("status")
        added = st.get("added")
        total = st.get("total_routes")
        prog = st.get("progress")
        msg = (st.get("message") or "")[:120]
        print(f"[{i*5}s] {status} {prog}% added={added} total={total} {msg}")
        if status in ("completed", "error"):
            break
    code, neighbors = req("GET", f"{base}/api/bgp/neighbors", timeout=180)
    rr = next((n for n in neighbors if n.get("neighbor_ip") == RR_IP), {})
    print(
        "after:",
        json.dumps(
            {k: rr.get(k) for k in ("session_state", "routes_received", "routes_sent", "routes_cached", "advertise_routes")},
            ensure_ascii=False,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
