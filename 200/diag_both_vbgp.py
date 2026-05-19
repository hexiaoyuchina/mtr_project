#!/usr/bin/env python3
import os
from pathlib import Path
import paramiko

for line in Path(__file__).resolve().parent.joinpath("lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()

pw = os.environ["MTR_OP_SSH_PASSWORD"]
script = """
export RR_ADDR=10.133.153.204 ROUTER_ID=10.133.153.200
bash /root/mtr_op/remote-network-prereq.sh
echo '=== route get ==='
ip route get 10.133.152.204 from 10.133.152.233 2>&1
ip route get 10.133.152.204 from 10.133.153.204 2>&1
ip -4 rule show | grep -E '45:|50:|233|204|152'
echo '=== links ==='
ip -br link show iv233 iv204 ens192
echo '=== tcp ==='
ss -tnp | grep 152.204
echo '=== neighbors ==='
curl -sf --max-time 8 http://127.0.0.1:9179/api/neighbors | python3 -m json.tool 2>/dev/null | grep -A12 152.204
curl -sf --max-time 8 http://127.0.0.1:9179/api/rr/status | python3 -c "import json,sys; d=json.load(sys.stdin); print('rr', d.get('rx_status',{}).get('rr_peers'))"
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.133.151.200", username="root", password=pw, timeout=45)
_, o, e = c.exec_command("bash -se", timeout=60)
o.channel.send(script.encode())
o.channel.shutdown_write()
print((o.read() + e.read()).decode("utf-8", "replace"))
c.close()
