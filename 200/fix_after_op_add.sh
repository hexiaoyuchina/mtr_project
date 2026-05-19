#!/bin/bash
set -e
VRF=vbgp10133153204
PEER=10.133.152.204
SPOOF=10.133.153.204
IV=iv204

ip link set ens192 up
ip link set ens224 up
bash /root/mtr_op/ensure_uplink_addrs.sh 2>/dev/null || true
sysctl -w net.ipv4.ip_nonlocal_bind=1 net.ipv4.tcp_l3mdev_accept=1

echo '=== remove default duplicate peer ==='
curl -sf -X DELETE "http://127.0.0.1:8808/api/bgp/neighbors/default/${PEER}" || echo del_default_fail

echo '=== re-add vbgp with bind iv204 ==='
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove \
  -H 'Content-Type: application/json' \
  -d "{\"address\":\"${PEER}\",\"vrf\":\"${VRF}\"}" || true
sleep 2
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/add \
  -H 'Content-Type: application/json' \
  -d "{\"address\":\"${PEER}\",\"remote_as\":63199,\"role\":\"downstream\",\"vrf\":\"${VRF}\",\"local_address\":\"${SPOOF}\",\"bind_interface\":\"${IV}\"}"
echo
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle \
  -H 'Content-Type: application/json' \
  -d "{\"address\":\"${PEER}\",\"vrf\":\"${VRF}\",\"enabled\":true}"
echo

# 201 侧 MAC on ens192
ip neigh replace ${PEER} lladdr 00:50:56:af:01:5a dev ${IV} nud permanent 2>/dev/null || true

echo '=== ipvlan ==='
ip -br addr show ${IV}
ip route show vrf ${VRF} | head -6
ip vrf exec ${VRF} ping -c2 -W2 ${PEER} || true

sleep 25
echo '=== tcp ==='
ss -tnp | grep ${PEER} || echo no_tcp
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='${PEER}':
    print(json.dumps(n,indent=2))
"
journalctl -u bgp-agent -n 20 --no-pager | grep -iE '${PEER}|${SPOOF}|error|estab|bind' | tail -12
