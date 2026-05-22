#!/usr/bin/env python3
import json
import os
from pathlib import Path

import paramiko

DEPLOY_DIR = Path(__file__).resolve().parent
for name in ("env", "env.example"):
    p = DEPLOY_DIR / name
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        if name == "env":
            break

script = r"""
echo '=== meta ==='
sqlite3 /root/mtr_op/data.db "SELECT vrf,neighbor_ip,role,source_ip,advertise_routes,store_received FROM bgp_neighbor_meta;" 2>/dev/null || echo empty
echo '=== agent neighbors ==='
curl -sS http://127.0.0.1:9179/api/neighbors | python3 -m json.tool 2>/dev/null | head -80
echo '=== op neighbors ==='
curl -sS http://127.0.0.1:8808/api/bgp/neighbors | python3 -m json.tool 2>/dev/null | head -80
echo '=== agent status rr ==='
curl -sS http://127.0.0.1:9179/api/rr/status 2>/dev/null | python3 -m json.tool | head -40
echo '=== env ROUTER_ID RR_ADDR ==='
grep -E 'ROUTER_ID|RR_ADDR' /etc/systemd/system/mtr-op.service /etc/systemd/system/mtr-op.service.d/* 2>/dev/null || true
systemctl show bgp-agent -p ExecStart --no-pager 2>/dev/null | head -3
echo '=== bgp-agent.env ==='
cat /root/mtr_op/data/bgp-agent.env 2>/dev/null || echo missing
echo '=== rx from /api/status ==='
curl -sS http://127.0.0.1:9179/api/status | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get("rx"), indent=2))'
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(
    os.environ["MTR_OP_HOST"],
    username="root",
    password=os.environ["MTR_OP_SSH_PASSWORD"],
    timeout=30,
    allow_agent=False,
    look_for_keys=False,
)
stdin, stdout, stderr = c.exec_command("bash -se", timeout=60)
stdin.write(script)
stdin.channel.shutdown_write()
print(stdout.read().decode(errors="replace"))
c.close()
