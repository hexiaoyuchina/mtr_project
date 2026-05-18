#!/usr/bin/env python3
"""验收 learned-routes：全部 / 仅 VRF / 指定 peer。"""
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.133.151.200", username="root", password="1234qwer", timeout=25, allow_agent=False, look_for_keys=False)
script = r"""
python3 <<'PY'
import json, urllib.request
base='http://127.0.0.1:8808'

def get(path):
    return json.load(urllib.request.urlopen(base+path, timeout=120))

cases = [
    ('all', '/api/bgp/learned-routes?page=1&page_size=5'),
    ('vrf_only', '/api/bgp/learned-routes?vrf=gobgp-rr&page=1&page_size=5'),
    ('nip_only', '/api/bgp/learned-routes?neighbor_ip=10.133.153.204&page=1&page_size=5'),
    ('or_both', '/api/bgp/learned-routes?vrf=gobgp-rr&neighbor_ip=10.133.152.204&page=1&page_size=5'),
]
for name, path in cases:
    j = get(path)
    print(name, 'total=', j.get('total'), 'routes=', len(j.get('routes') or []))
PY
"""
_, o, e = c.exec_command("bash -se", timeout=120)
o.channel.send(script.encode())
o.channel.shutdown_write()
print(o.read().decode())
if e.read().decode().strip():
    print(e.read().decode())
c.close()
