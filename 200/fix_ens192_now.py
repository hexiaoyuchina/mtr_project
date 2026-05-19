#!/usr/bin/env python3
import time
import paramiko

PW = "1234qwer"
REMOTE = "/root/mtr_op"


def run(host: str, script: str, timeout: int = 120) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=PW, timeout=45)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    return (o.read() + e.read()).decode("utf-8", "replace")


print("=== 200 ens192 + DNAT ===")
print(
    run(
        "10.133.151.200",
        f"""
set -e
export RR_ADDR=10.133.153.204 MTR_BGP_RR_UPLINK_IFACE=ens224
export MTR_BGP_SAT_DNAT_IIF=1 MTR_BGP_IPVLAN_AUTO=1 MTR_BGP_IPVLAN_BASE_IFACE=ens192
export MTR_BGP_PEER_NEIGH_MAC_10_133_152_204=00:50:56:af:01:5a
ip link set ens192 up
ip link set iv204 up
cd {REMOTE} && ./venv/bin/python3 -c "
from pathlib import Path
from app import bgp_ipvlan_reconcile
db = Path('{REMOTE}/data.db')
print('should_dnat', bgp_ipvlan_reconcile.should_satellite_dnat_spoof_ip('10.133.153.204', 'ens192'))
print(bgp_ipvlan_reconcile.reconcile_vrf_from_op_database(db, 'vbgp10133153204', peer_ip='10.133.152.204'))
print(bgp_ipvlan_reconcile.reconcile_satellite_dnat(db))
"
nft list chain inet mtr_bgp_sat_dnat prerouting 2>/dev/null | head -6
ip neigh replace 10.133.152.204 lladdr 00:50:56:af:01:5a dev iv204 nud permanent
ping -c2 -W2 -I 10.133.153.204 10.133.152.204 || true
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove -H 'Content-Type: application/json' -d '{{"address":"10.133.152.204","vrf":"vbgp10133153204"}}' || true
sleep 2
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/add -H 'Content-Type: application/json' -d '{{"address":"10.133.152.204","remote_as":63199,"role":"downstream","vrf":"vbgp10133153204","local_address":"10.133.153.204","bind_interface":"iv204","passive_mode":true}}'
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle -H 'Content-Type: application/json' -d '{{"address":"10.133.152.204","vrf":"vbgp10133153204","enabled":true}}'
""",
    )
)

print("\n=== 201 ens192 回程 ===")
print(
    run(
        "10.133.151.201",
        """
ip link set ens192 up
ip route del 10.133.153.0/24 via 10.133.152.200 2>/dev/null || true
ip route replace 10.133.153.0/24 dev ens192 scope link
ip route replace 10.133.153.204/32 dev ens192 scope link
ip neigh replace 10.133.153.204 lladdr 00:50:56:af:97:a6 dev ens192 nud permanent
sysctl -w net.ipv4.conf.ens192.rp_filter=0
ip route | grep 153
ping -c2 -W2 10.133.153.204
vtysh -c 'clear ip bgp 10.133.153.204' 2>/dev/null || true
""",
        timeout=30,
    )
)

print("\n等待 25s…")
time.sleep(25)
print(
    run(
        "10.133.151.200",
        """
ss -tnp | grep 152.204 | head -6 || echo no_tcp
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='10.133.152.204':
    print(n)
"
""",
        timeout=30,
    )
)
print(
    run(
        "10.133.151.201",
        "vtysh -c 'show bgp summary' 2>/dev/null | grep 153.204 || true",
        timeout=20,
    )
)
