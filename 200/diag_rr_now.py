#!/usr/bin/env python3
import os
from pathlib import Path
import paramiko

LAB = Path(__file__).resolve().parent
for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()

pw = os.environ["MTR_OP_SSH_PASSWORD"]
script = """
echo '=== links ==='
ip -br link show ens192 ens224 ens160 2>/dev/null || true
ip -br addr show ens224 ens192 2>/dev/null || true
bash /root/mtr_op/remote-network-prereq.sh 2>/dev/null || true
ip route get 10.133.153.204 from 10.133.153.200 2>&1
echo '=== ping ==='
ping -c2 -W2 -I 10.133.153.200 10.133.153.204 2>&1
ping -c2 -W2 -I ens224 10.133.153.204 2>&1
echo '=== tcp 179 ==='
ss -tnp | grep 153.204 | head -10
echo '=== agent ==='
curl -sf --max-time 8 http://127.0.0.1:9179/api/rr/status 2>/dev/null | head -c 800
echo
curl -sf --max-time 8 http://127.0.0.1:9179/api/neighbors 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
for n in d.get('neighbors',[]):
  if n.get('address')=='10.133.153.204':
    print('agent', n)
"
echo '=== dnat ens192 on 153.204? ==='
nft list chain inet mtr_bgp_sat_dnat prerouting 2>/dev/null | grep 153.204 || true
echo '=== journal ==='
journalctl -u bgp-agent -n 30 --no-pager 2>/dev/null | grep -iE '153.204|rr|179' | tail -12
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.133.151.200", username="root", password=pw, timeout=45)
_, o, e = c.exec_command("bash -se", timeout=90)
o.channel.send(script.encode())
o.channel.shutdown_write()
print((o.read() + e.read()).decode("utf-8", "replace"))
c.close()
