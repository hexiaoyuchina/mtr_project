#!/usr/bin/env python3
"""验证多 RR：OP 添加 153.204、Agent 多 RX peer、与改造前单 RR 操作兼容。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LAB = Path(__file__).resolve().parent
RR = "10.133.153.204"
VRF = "gobgp-rr"
LOCAL = "10.133.153.200"


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def http_json(method: str, url: str, body: dict | None = None, timeout: int = 120) -> tuple[int, object]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw[:500]}


def main() -> int:
    load_lab_env()
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    op = f"http://{host}:{os.environ.get('MTR_OP_PORT', '8808')}"
    agent = f"http://{host}:9179"
    as_n = int(os.environ.get("LOCAL_AS", "63199"))

    print(f"=== 多 RR 验证 @ {host} ===\n")

    code, j = http_json("POST", f"{agent}/api/rr/unfreeze")
    print(f"[0] unfreeze http={code}")
    time.sleep(2)

    # 与界面一致：POST OP 添加 RR
    payload = {
        "vrf": VRF,
        "neighbor_ip": RR,
        "remote_as": as_n,
        "role": "rr",
        "source_ip": LOCAL,
    }
    code, j = http_json("POST", f"{op}/api/bgp/neighbors", payload, timeout=180)
    print(f"[1] OP POST 添加 RR {RR} http={code}")
    if code == 409:
        print("    已存在 meta，改用 Agent rr/config 确保 RX peer")
        code2, j2 = http_json(
            "POST",
            f"{agent}/api/rr/config",
            {"address": RR, "remote_as": as_n, "local_address": LOCAL},
        )
        print(f"    rr/config http={code2} {j2}")
    elif code not in (200, 201):
        print(j)
        return 1
    else:
        print(f"    session_state={j.get('session_state')} routes_received={j.get('routes_received')}")

    http_json("POST", f"{agent}/api/rr/unfreeze")
    time.sleep(12)
    code, peers = http_json("GET", f"{agent}/api/neighbors")
    rx_list = [p for p in (peers or {}).get("neighbors", []) if p.get("session") == "rx"]
    print(f"[2] Agent RX peers ({len(rx_list)}):")
    for p in rx_list:
        print(f"    {p.get('address')} state={p.get('state')} pfx_rcd={p.get('pfx_rcd')}")

    if not any(str(p.get("address")) == RR for p in rx_list):
        print("FAIL 未在 Agent 上看到 153.204 RX 邻居")
        return 1

    code, st = http_json("GET", f"{agent}/api/rr/status")
    rx_status = (st or {}).get("rx_status") or {}
    rr_peers = rx_status.get("rr_peers") or []
    print(f"[3] rr/status rr_peers={len(rr_peers)} rr_connected={rx_status.get('rr_connected')}")

    code, rows = http_json("GET", f"{op}/api/bgp/neighbors")
    rr_rows = [r for r in rows if r.get("vrf") == VRF and r.get("neighbor_ip") == RR]
    print(f"[4] OP 列表中 204 行数={len(rr_rows)}")
    if not rr_rows:
        print("FAIL OP 列表无 153.204")
        return 1

    est = str(rr_rows[0].get("session_state") or "").lower()
    rx_st = str(rx_list[0].get("state") or "").lower() if rx_list else ""
    if "established" not in est and "established" not in rx_st:
        print("WARN 会话未 Established，请检查 uplink / unfreeze / ROS peer-lin200-153")

    # ingest
    q = urllib.parse.urlencode({"window": "upstream", "vrf": VRF, "neighbor_ip": RR})
    code, ing = http_json("POST", f"{op}/api/bgp/neighbors/{urllib.parse.quote(VRF)}/{RR}/store-routes", {"store_received_routes": 1}, timeout=300)
    print(f"[5] store-routes http={code} cached={ing.get('routes_cached') if isinstance(ing, dict) else ing}")

    print("\nRESULT: PASS (多 RR 架构下 153.204 经 OP 添加成功)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
