#!/usr/bin/env python3
import paramiko

PW = "1234qwer"
script = r"""
set -x
echo '--- test TCP 179 to RR from 153.200 ---'
timeout 2 bash -c ': > /dev/tcp/10.133.153.204/179' 2>/dev/null && echo tcp179_ok || echo tcp179_fail
ss -tn state established '( dport = :179 or sport = :179 )' | grep 153.204 | grep 153.200 || echo no_estab_200_204
echo '--- listen 179 ---'
ss -lnpt | grep ':179'
echo '--- syn-sent ---'
ss -tnp state syn-sent | grep 153.204 || true
echo '--- restart rr peer on agent ---'
curl -sf -X POST http://127.0.0.1:9179/api/rr/remove
curl -sf -X POST http://127.0.0.1:9179/api/rr/config \
  -H 'Content-Type: application/json' \
  -d '{"address":"10.133.153.204","remote_as":63199,"local_address":"10.133.153.200"}'
sleep 12
ss -tnp | grep -E '153.200|153.204' | grep 179 || true
curl -s http://127.0.0.1:9179/api/rr/status
echo
journalctl -u bgp-agent -n 15 --no-pager | tail -15
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.133.151.200", username="root", password=PW, timeout=25, allow_agent=False, look_for_keys=False)
_, o, e = c.exec_command("bash -se", timeout=120)
o.channel.send(script.encode())
o.channel.shutdown_write()
print(o.read().decode("utf-8", "replace"))
print(e.read().decode("utf-8", "replace"))
c.close()

# Enable ROS peer if disabled
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.133.151.210", username="admin", password=PW, timeout=25, allow_agent=False, look_for_keys=False)
_, o, e = c.exec_command("/routing bgp peer enable [find name=peer-lin200-153]", timeout=30)
print("ROS enable:", o.read().decode(), e.read().decode())
_, o, e = c.exec_command("/routing bgp peer print detail where name=peer-lin200-153", timeout=30)
print(o.read().decode())
c.close()
