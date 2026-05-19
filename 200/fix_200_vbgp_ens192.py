#!/usr/bin/env python3
"""Linux 200：修复 vbgp/ens192 下游 — bind iv204、去掉 default 重复邻居、nonlocal_bind。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import paramiko

H200 = "10.133.151.200"
VRF = "vbgp10133153204"
PEER = "10.133.152.204"
SPOOF = "10.133.153.204"
IV = "iv204"


def load_env() -> str:
    for line in Path(__file__).resolve().parent.joinpath("lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def run(pw: str, script: str, timeout: int = 180) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H200, username="root", password=pw, timeout=45, allow_agent=False, look_for_keys=False, banner_timeout=45)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    return (o.read() + e.read()).decode("utf-8", "replace")


def main() -> int:
    pw = load_env()
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
    print(
        run(
            pw,
            f"""
set -e
REMOTE={remote}
export MTR_OP_REMOTE_DIR=$REMOTE
export MTR_OP_DB=$REMOTE/data.db
export MTR_BGP_IPVLAN_AUTO=1
export MTR_BGP_IPVLAN_BASE_IFACE=ens192
export ROUTER_ID=10.133.153.200
export RR_ADDR=10.133.153.204
export LOCAL_AS=63199
export MTR_DOWNSTREAM_REMOTE_AS=63199

echo '=== link up ==='
ip link set ens192 up
ip link set ens224 up
[ -f $REMOTE/ensure_uplink_addrs.sh ] && bash $REMOTE/ensure_uplink_addrs.sh || true

echo '=== ipvlan reconcile ==='
cd $REMOTE && ./venv/bin/python3 -c "
from pathlib import Path
from app import bgp_ipvlan_reconcile
db = Path('$REMOTE/data.db')
print(bgp_ipvlan_reconcile.reconcile_vrf_from_op_database(db, '{VRF}', peer_ip='{PEER}'))
print('ivlan_iface', bgp_ipvlan_reconcile.ipvlan_iface_for_vrf(db, '{VRF}'))
print('tx_port', bgp_ipvlan_reconcile.tx_listen_port_for_vrf('{VRF}'))
print('should_dnat', bgp_ipvlan_reconcile.should_satellite_dnat_spoof_ip('{SPOOF}'))
print(bgp_ipvlan_reconcile.reconcile_satellite_dnat(db))
" 2>&1 || true

sysctl -w net.ipv4.ip_nonlocal_bind=1

echo '=== remove duplicate default peer ==='
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove \\
  -H 'Content-Type: application/json' \\
  -d '{{"address":"{PEER}","vrf":"default"}}' && echo removed_default || echo no_default

echo '=== re-add vbgp with bind_interface ==='
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove \\
  -H 'Content-Type: application/json' \\
  -d '{{"address":"{PEER}","vrf":"{VRF}"}}' || true
sleep 2
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/add \\
  -H 'Content-Type: application/json' \\
  -d '{{
    "address":"{PEER}",
    "remote_as":63199,
    "role":"downstream",
    "vrf":"{VRF}",
    "local_address":"{SPOOF}",
    "bind_interface":"{IV}",
    "passive_mode":false
  }}'
echo
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle \\
  -H 'Content-Type: application/json' \\
  -d '{{"address":"{PEER}","vrf":"{VRF}","enabled":true}}'
echo

echo 'wait 20s'
sleep 20
echo '=== result ==='
ip -br link show ens192 {IV}
ss -tnp | grep {PEER} || echo no_tcp
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='{PEER}':
    print(json.dumps(n,indent=2))
"
nft list table inet mtr_bgp_sat_dnat 2>/dev/null | head -12
""",
            timeout=240,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
