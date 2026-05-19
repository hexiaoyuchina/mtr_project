#!/usr/bin/env python3
"""诊断 201 mtr 第 2 跳 ???：200 上 NFQUEUE / NAT / 守护进程 / hop 规则。"""
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


def ssh(host: str, script: str, timeout: int = 90) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def api(path: str) -> dict:
    req = urllib.request.Request(
        f"http://{H200}:8808{path}",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


print("=== OP hop rules / global ===")
try:
    g = api("/api/global")
    print("hijack_enabled:", g.get("hijack_enabled"))
    rules = api("/api/hop-rules")
    for r in rules:
        print(f"  id={r.get('id')} enabled={r.get('enabled')} match={r.get('match_cidr')} forged={r.get('forged_src')}")
except Exception as ex:
    print("api_err:", ex)

print("\n=== 200: processes / map / iptables counters ===")
print(
    ssh(
        H200,
        """
ps aux | grep -E 'te_rewrite|uvicorn' | grep -v grep
echo '--- map ---'
cat /tmp/mtr_te_map.env 2>/dev/null || echo '(no map)'
echo '--- tail log ---'
tail -15 /tmp/te_rewrite_nfqueue.log 2>/dev/null || echo '(no log)'
echo '--- nat ---'
iptables -t nat -L -n -v | grep -E '152.204|152.200|Chain'
echo '--- mangle FORWARD ---'
iptables -t mangle -L FORWARD -n -v --line-numbers | head -12
echo '--- sysctl rp_filter ---'
sysctl net.ipv4.conf.all.rp_filter net.ipv4.conf.ens224.rp_filter net.ipv4.conf.ens192.rp_filter
""",
    )
)

print("\n=== 201: short mtr ===")
print(
    ssh(
        H201,
        "mtr -4 -r -n -m 8 -c 3 -a 10.133.152.204 -I ens192 210.73.209.82 2>&1 | tail -12",
        timeout=120,
    )
)
