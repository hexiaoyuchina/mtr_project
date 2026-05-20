#!/bin/bash
RR=139.159.43.249
echo "=== gobgp neighbor ==="
curl -sf http://127.0.0.1:8808/api/bgp/neighbors 2>/dev/null | python3 -m json.tool 2>/dev/null | head -80
echo
echo "=== agent status ==="
curl -sf http://127.0.0.1:9179/api/status 2>/dev/null | python3 -m json.tool 2>/dev/null | head -60
echo
echo "=== upstream rib all vrfs count ==="
for vrf in default gobgp-rr; do
  echo -n "vrf=$vrf "
  curl -sf "http://127.0.0.1:9179/api/rib/routes/count?window=upstream&vrf=${vrf}&neighbor_ip=${RR}" 2>/dev/null || echo fail
done
echo
echo "=== arp 249 on uplink ==="
ip neigh show "$RR" dev enp59s0f0np0 2>/dev/null
echo
echo "=== sample uplink packet L2 (1 pkt) ==="
timeout 8 tcpdump -ni enp59s0f0np0 -ee -c 3 'host 8.8.8.8 and host 139.159.43.208' 2>&1
