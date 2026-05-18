#!/usr/bin/env python3
"""重建 235 卫星 BGP：清 legacy 路由 + 删加邻居（绑 iv235）。"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

SPOOF = "10.133.152.235"
VRF = "vbgp10133152235"
PEER = "10.133.152.204"
AS = 63199


def load_env() -> None:
    for line in (Path(__file__).parent / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def api(method: str, path: str, body: dict | None = None) -> dict:
    base = f"http://{os.environ.get('MTR_OP_HOST', '10.133.151.200')}:8808"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else {}


def main() -> None:
    load_env()
    print("1. ipvlan reconcile (purge legacy default in vrf)")
    print(json.dumps(api("POST", "/api/bgp/ipvlan-satellites/reconcile", {}), indent=2)[:2000])

    print("\n2. delete BGP neighbor 235")
    try:
        api("DELETE", f"/api/bgp/neighbors/{VRF}/{PEER}")
        print("deleted")
    except urllib.error.HTTPError as e:
        print("delete:", e.read().decode()[:300])

    time.sleep(1)

    print("\n3. re-add BGP neighbor with source", SPOOF)
    body = {
        "vrf": VRF,
        "neighbor_ip": PEER,
        "remote_as": AS,
        "role": "downstream",
        "source_ip": SPOOF,
        "bgp_local_as": AS,
        "bgp_router_id": SPOOF,
        "create_kernel_vrf_if_missing": True,
    }
    print(json.dumps(api("POST", "/api/bgp/neighbors", body), indent=2, ensure_ascii=False))

    time.sleep(5)
    print("\n4. verify")
    for n in api("GET", "/api/bgp/neighbors"):
        if n.get("vrf") == VRF:
            print("OP:", n.get("source_ip"), n.get("session_state"))


if __name__ == "__main__":
    main()
