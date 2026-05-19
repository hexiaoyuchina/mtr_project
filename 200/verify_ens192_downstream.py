#!/usr/bin/env python3
import paramiko

H200, H201 = "10.133.151.200", "10.133.151.201"
PW = "1234qwer"
REMOTE = "/root/mtr_op"


def run(host: str, script: str, timeout: int = 90) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=PW, timeout=45)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


py = f"""
from pathlib import Path
from app import bgp_ipvlan_reconcile
db = Path('{REMOTE}/data.db')
print('should_dnat', bgp_ipvlan_reconcile.should_satellite_dnat_spoof_ip('10.133.153.204', 'ens192'))
print(bgp_ipvlan_reconcile.reconcile_satellite_dnat(db))
"""

print(run(H200, f"""
export RR_ADDR=10.133.153.204 MTR_BGP_RR_UPLINK_IFACE=ens224 MTR_BGP_SAT_DNAT_IIF=1
cd {REMOTE} && ./venv/bin/python3 -c {repr(py)}
nft list chain inet mtr_bgp_sat_dnat prerouting 2>/dev/null | head -8
ping -c2 -W2 -I 10.133.153.204 10.133.152.204 || true
ss -tnp | grep 152.204 | head -5 || echo no_tcp
curl -sf http://127.0.0.1:9179/api/neighbors 2>/dev/null | python3 -c "import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='10.133.152.204': print(n.get('vrf'), n.get('state'), n.get('pfx_rcd'))"
"""))

print(run(H201, """
ip route show dev ens192 | head -5
ping -c2 -W2 -c1 10.133.153.204 2>/dev/null || ping -c1 -W2 10.133.153.204
vtysh -c 'show bgp summary' 2>/dev/null | grep 153.204 || true
""", timeout=30))
