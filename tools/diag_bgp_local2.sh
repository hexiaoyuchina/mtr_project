#!/bin/bash
PEER=139.159.43.208
SPOOF=139.159.43.249
VRF=vbgp13915943249

echo "=== vrf routes ==="
ip vrf exec $VRF ip route
echo "=== main route to 208 ==="
ip route get $PEER from $SPOOF iif iv249 2>&1 || ip route get $PEER from $SPOOF
echo "=== arp/neigh 208 ==="
ip neigh show $PEER dev enp59s0f0np0
arping -c 2 -I enp59s0f0np0 $PEER 2>&1 || true
echo "=== listen ports tx ==="
ss -tlnp | grep -E '1790|1830|1831|bgp_agent'
echo "=== manual tcp from 249 ==="
timeout 3 ip vrf exec $VRF nc -zv -s $SPOOF $PEER 179 2>&1 || \
timeout 3 nc -zv -s $SPOOF $PEER 179 2>&1 || true
echo "=== tcpdump during nc ==="
(timeout 5 tcpdump -ni enp59s0f0np0 -c 10 host $PEER 2>&1) &
sleep 1
timeout 3 ip vrf exec $VRF bash -c "echo | nc -w2 -s $SPOOF $PEER 179" 2>&1 || true
wait
echo "=== gobgp log grep 208 ==="
journalctl -u bgp-agent --since "5 min ago" --no-pager 2>/dev/null | grep -i 208 || true
