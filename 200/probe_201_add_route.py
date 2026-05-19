#!/usr/bin/env python3
"""在 Linux 201 FRR 上通告一条测试路由，并在 200 上查看下游是否学到。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H200, H201 = "10.133.151.200", "10.133.151.201"
TEST_PFX = "203.0.113.201/32"


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def pw() -> str:
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def root(host: str, script: str, timeout: int = 120) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw(), timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def http_json(url: str, method: str = "GET", body: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode() if body else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}


def main() -> int:
    load_lab_env()
    agent = f"http://{H200}:9179"
    op = f"http://{H200}:{os.environ.get('MTR_OP_PORT', '8808')}"

    print(f"=== 1) Linux {H201} 当前 BGP ===\n")
    print(
        root(
            H201,
            """
vtysh -c 'show bgp ipv4 unicast summary' 2>/dev/null | head -20
echo '--- running bgp ---'
vtysh -c 'show running-config' 2>/dev/null | grep -E 'router bgp| network |neighbor ' | head -30
""",
        )
    )

    print(f"\n=== 2) 在 {H201} 通告 {TEST_PFX} ===\n")
    print(
        root(
            H201,
            f"""
set -e
TEST='{TEST_PFX}'
vtysh <<'VTY'
configure terminal
ip route 203.0.113.201/32 Null0
router bgp 63199
 address-family ipv4 unicast
  network 203.0.113.201/32
 exit-address-family
end
write memory
VTY
echo '--- show bgp prefix ---'
vtysh -c 'show bgp ipv4 203.0.113.201/32' 2>/dev/null | head -15
echo '--- summary ---'
vtysh -c 'show bgp ipv4 unicast summary' 2>/dev/null | head -12
echo '--- neighbor 152.200 ---'
vtysh -c 'show bgp neighbors 10.133.152.200' 2>/dev/null | grep -iE 'BGP state|Notification|update-source|Prefixes|Accepted' | head -12
echo '--- neighbor 153.204 ---'
vtysh -c 'show bgp neighbors 10.133.153.204' 2>/dev/null | grep -iE 'BGP state|Prefixes' | head -8
""",
        )
    )

    print("\n等待 200 学路由 (10s) …")
    time.sleep(10)

    print(f"\n=== 3) Linux {H200} 观测 ===\n")
    try:
        fs = http_json(f"{agent}/api/peers/freeze-status")
        for p in fs.get("downstream") or []:
            if "152.204" in str(p.get("neighbor_ip")):
                print("Agent downstream peer:", json.dumps(p, ensure_ascii=False))
    except Exception as ex:
        print("freeze-status error:", ex)

    ds_vrf = "vbgp10133153204"
    for vrf in (ds_vrf, "default"):
        q = urllib.parse.urlencode(
            {"window": "downstream", "vrf": vrf, "neighbor_ip": "10.133.152.204"}
        )
        try:
            cnt = http_json(f"{agent}/api/rib/routes/count?{q}")
            print(f"downstream RIB {vrf}/152.204: count={cnt.get('count')}")
        except Exception as ex:
            print(f"count {vrf}:", ex)

    try:
        neighbors = http_json(f"{op}/api/bgp/neighbors")
        row = None
        for n in neighbors if isinstance(neighbors, list) else []:
            if str(n.get("neighbor_ip")) == "10.133.152.204" and str(n.get("vrf")) == ds_vrf:
                row = n
        if row:
            print(
                "OP peer:",
                f"state={row.get('session_state')}",
                f"rx={row.get('routes_received')}",
                f"cached={row.get('routes_cached')}",
                f"store={row.get('store_received_routes')}",
            )
            if int(row.get("store_received_routes") or 0):
                ing = http_json(
                    f"{op}/api/bgp/learned-routes/ingest?vrf={urllib.parse.quote(ds_vrf)}&neighbor_ip=10.133.152.204",
                    "POST",
                )
                print("ingest:", ing)
    except Exception as ex:
        print("OP error:", ex)

    time.sleep(2)
    q = urllib.parse.urlencode(
        {
            "window": "downstream",
            "vrf": ds_vrf,
            "neighbor_ip": "10.133.152.204",
            "page": 1,
            "page_size": 50,
        }
    )
    try:
        data = http_json(f"{agent}/api/rib/routes?{q}")
        hits = [
            r
            for r in (data.get("routes") or [])
            if "203.0.113.201" in str(r.get("prefix", ""))
        ]
        print(f"RIB page total={data.get('total')} contains {TEST_PFX}: {bool(hits)}")
        if hits:
            print(" ", hits[0])
    except Exception as ex:
        print("rib routes:", ex)

    print(root(H200, "curl -sf http://127.0.0.1:9179/api/peers/freeze-status | head -c 2000; echo"))

    print("\n=== 解读 ===")
    print(f"- 201 已加 Null0 + BGP network {TEST_PFX}（AS 63199）")
    print("- 若 201 邻居仍为 Active，需先恢复 200↔201 BGP Established，200 才能学到该前缀")
    print("- Established 后：下游入库 → RR 行聚合通告 → ROS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
