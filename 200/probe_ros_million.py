#!/usr/bin/env python3
"""连 ROS 210 + 200/201，核对百万级通告是否到下游。"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H200, H201, H210 = "10.133.151.200", "10.133.151.201", "10.133.151.210"
SOURCE_RR = "10.133.153.204"
TARGET_PEER = "10.133.152.204"


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def pw() -> str:
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def ros(cmd: str, timeout: int = 90) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H210, username="admin", password=pw(), timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command(cmd if cmd.startswith("/") else f"/{cmd}", timeout=timeout)
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


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


def http_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}


def main() -> int:
    load_lab_env()
    op = f"http://{H200}:{os.environ.get('MTR_OP_PORT', '8808')}"
    agent = f"http://{H200}:9179"

    print(f"=== RouterOS {H210} (BGP 身份 {SOURCE_RR} → 200/{SOURCE_RR.replace('204','200')}) ===\n")
    ros_cmds = [
        "/routing bgp connection print detail without-paging",
        "/routing bgp session print stats without-paging",
        "/routing route print count-only where protocol=bgp",
        "/routing bgp network print count-only",
    ]
    for cmd in ros_cmds:
        print(f"--- {cmd}")
        print(ros(cmd)[:4000])
        print()

    print(f"=== Linux {H200} 持久库 / 通告 ===\n")
    q = urllib.parse.urlencode(
        {"window": "upstream", "vrf": "gobgp-rr", "neighbor_ip": SOURCE_RR}
    )
    try:
        cnt = http_get(f"{agent}/api/rib/routes/count?{q}")
        print(f"Agent 从 RR 入库条数 (153.204): {cnt.get('count')}")
    except Exception as ex:
        print(f"rib count error: {ex}")

    try:
        rr = http_get(f"{agent}/api/rr/status")
        print(f"RR 会话: {json.dumps(rr, ensure_ascii=False)}")
    except Exception as ex:
        print(f"rr status error: {ex}")

    try:
        neighbors = http_get(f"{op}/api/bgp/neighbors")
        for n in neighbors if isinstance(neighbors, list) else []:
            nip = str(n.get("neighbor_ip") or "")
            if nip in (SOURCE_RR, TARGET_PEER):
                print(
                    f"OP 邻居 {n.get('vrf')}/{nip}: "
                    f"state={n.get('session_state')} "
                    f"rx={n.get('routes_received')} tx_sent={n.get('routes_sent')} "
                    f"cached={n.get('routes_cached')} advertise={n.get('advertise_routes')}"
                )
    except Exception as ex:
        print(f"neighbors error: {ex}")

    print(root(H200, r"""
curl -s http://127.0.0.1:9179/api/peers/freeze-status | python3 -m json.tool 2>/dev/null | head -60
echo '--- TX peers 152.204 ---'
curl -s http://127.0.0.1:9179/api/neighbors | python3 <<'PY'
import json, sys
d = json.load(sys.stdin)
for n in d.get("neighbors") or []:
    if "152.204" in str(n.get("address", "")):
        print(json.dumps(n, ensure_ascii=False))
PY
"""))

    print(f"\n=== Linux {H201} FRR（下游 {TARGET_PEER} 侧）===\n")
    print(
        root(
            H201,
            r"""
echo '--- BGP summary ---'
vtysh -c 'show ip bgp summary' 2>/dev/null || true
vtysh -c 'show bgp ipv4 unicast summary' 2>/dev/null || true
for nip in 10.133.153.204 10.133.153.200 10.133.152.200; do
  echo "--- show bgp neighbors $nip ---"
  vtysh -c "show bgp neighbors $nip" 2>/dev/null | grep -iE 'BGP state|Hostname|foreign|local|received|sent|Prefix|Accepted' | head -20
done
echo '--- established :179 ---'
ss -tn state established '( sport = :179 or dport = :179 )' | head -20 || true
""",
        )
    )

    print("\n=== 解读 ===")
    print("- ROS「发出」路由数：看 session/connection 的 prefix 统计或 bgp network 条数")
    print("- 200 是否已通告下游：OP 行 routes_sent (pfx_adv) 或 Agent TX peer pfx_adv")
    print("- 201 是否「收到」百万：vtysh neighbors 里 Prefix Received / pfxRcd")
    return 0


if __name__ == "__main__":
    sys.exit(main())
