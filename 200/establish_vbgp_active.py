#!/usr/bin/env python3
"""ens192 下游：200 主动连 152.204:179，201 侧 neighbor 改 passive。"""
import time
import paramiko

PW = "1234qwer"


def run(host, script, timeout=90):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=PW, timeout=45)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    return (o.read() + e.read()).decode("utf-8", "replace")


print(run("10.133.151.201", """
vtysh <<'VTY'
configure terminal
router bgp 63199
 address-family ipv4 unicast
  neighbor 10.133.153.204 passive
 exit-address-family
exit
write memory
VTY
vtysh -c 'show bgp neighbors 10.133.153.204' | grep -i passive || true
vtysh -c 'clear ip bgp 10.133.153.204' 2>/dev/null || true
""", timeout=40))

print(run("10.133.151.200", """
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove -H 'Content-Type: application/json' \\
  -d '{"address":"10.133.152.204","vrf":"vbgp10133153204"}' || true
sleep 2
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/add -H 'Content-Type: application/json' \\
  -d '{"address":"10.133.152.204","remote_as":63199,"role":"downstream","vrf":"vbgp10133153204","local_address":"10.133.153.204","bind_interface":"iv204","passive_mode":false}'
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle -H 'Content-Type: application/json' \\
  -d '{"address":"10.133.152.204","vrf":"vbgp10133153204","enabled":true}'
ping -c1 -W2 -I 10.133.153.204 10.133.152.204
"""))

time.sleep(20)
print(run("10.133.151.200", """
ss -tnp | grep 152.204 | head -6
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='10.133.152.204': print(n)
"
journalctl -u bgp-agent -n 15 --no-pager | grep 152.204 | tail -8
""", timeout=40))

print(run("10.133.151.201", "vtysh -c 'show bgp summary' | grep 153.204", timeout=20))
