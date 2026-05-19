#!/usr/bin/env python3
"""ARP 引流 + BGP + 201 路由/邻居 全量诊断。"""
import json
import os
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

pw = os.environ["MTR_OP_SSH_PASSWORD"]
H200, H201 = "10.133.151.200", "10.133.151.201"


def api(path):
    req = urllib.request.Request(
        f"http://{H200}:8808{path}", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode())


def ssh(host, script, timeout=90):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    return (o.read() + e.read()).decode("utf-8", "replace")


print("=== OP ===")
try:
    print("health:", api("/health"))
    print("arp:", api("/api/arp-spoof/settings"))
    for t in api("/api/arp-spoof/targets"):
        print("  target:", json.dumps(t, ensure_ascii=False))
    nb = api("/api/bgp/neighbors")
    if isinstance(nb, dict):
        for n in nb.get("neighbors", []):
            print(
                "  bgp:",
                n.get("vrf"),
                n.get("address"),
                n.get("state"),
                "src",
                n.get("local_address") or n.get("source_ip"),
            )
except Exception as ex:
    print("OP err:", ex)

print("\n=== 200 kernel ===")
print(
    ssh(
        H200,
        """
systemctl status mtr-op --no-pager 2>&1 | head -6
pgrep -af uvicorn | head -2
ps aux | grep -E 'arp_spoof|bgp_agent' | grep -v grep | head -4
echo '--- ens192 ---'
ip -br addr show ens192
echo '--- iv204 iv249 ---'
for i in iv204 iv249; do ip link show $i 2>&1; ip -br addr show $i 2>&1; done
echo '--- grep 152 ---'
ip addr | grep '152\\.' || true
echo '--- nft dnat ---'
nft list table inet mtr_bgp_sat_dnat 2>/dev/null | head -18 || echo no_sat_dnat
curl -sf http://127.0.0.1:9179/api/neighbors 2>/dev/null | head -c 2500 || echo agent_down
""",
    )
)

print("\n=== 201 ===")
print(
    ssh(
        H201,
        """
ip addr show ens192 | head -10
ip route show table all | grep 152.249 || echo no_249_in_routes
ip rule list | head -12
echo '--- route get variants ---'
ip route get 10.133.152.249 2>&1
ip route get 10.133.152.249 from 10.133.152.204 2>&1
ip route get 10.133.152.249 from 10.133.152.204 iif ens192 2>&1
ip neigh show 10.133.152.249 dev ens192 2>/dev/null || true
ip neigh show 10.133.153.204 dev ens192 2>/dev/null || true
vtysh -c 'show bgp summary' 2>/dev/null | grep -E '152\\.|153\\.' || true
ping -c2 -W2 -a 10.133.152.204 -I ens192 10.133.152.249 2>&1 | tail -5
ping -c2 -W2 -a 10.133.152.204 -I ens192 10.133.153.204 2>&1 | tail -5
""",
        timeout=45,
    )
)
