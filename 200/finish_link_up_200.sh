#!/bin/bash
set -e
ip link set ens192 up
ip link set ens224 up
if ! ip -4 addr show dev ens224 | grep -q '10.133.153.200/'; then
  ip addr add 10.133.153.200/32 dev ens224 2>/dev/null || true
fi
bash /root/mtr_op/remote-network-prereq.sh
echo "addrs:"
ip -br addr show ens224 ens192 iv204 2>/dev/null || true
ip route get 10.133.153.204 from 10.133.153.200 2>&1 || true
curl -sf -X POST http://127.0.0.1:9179/api/rr/config -H 'Content-Type: application/json' \
  -d '{"address":"10.133.153.204","remote_as":63199,"local_address":"10.133.153.200"}'
curl -sf -X POST http://127.0.0.1:9179/api/rr/unfreeze
echo
echo "wait 55s for 201 BGP retry..."
sleep 55
echo "tcp:"
ss -tnp | grep -E '152.204|153.204' | head -15
echo "neighbors:"
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  print(n.get('vrf'), n.get('address'), n.get('state'), n.get('pfx_rcd'))
"
