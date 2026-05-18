#!/bin/bash
# 修复 vbgp10133153204 / 10.133.152.204 启停后无法 Established
set -e
VRF=vbgp10133153204
PEER=10.133.152.204
SPOOF=10.133.153.204
OP=http://127.0.0.1:8808

echo "=== DNAT reconcile ==="
cd /root/mtr_op
./venv/bin/python3 -c "
from pathlib import Path
from app import bgp_ipvlan_reconcile
print(bgp_ipvlan_reconcile.reconcile_satellite_dnat(Path('/root/mtr_op/data.db')))
"

echo "=== nft rules for $SPOOF ==="
nft list chain inet mtr_bgp_sat_dnat prerouting 2>/dev/null | grep -E '153.204|1833' || true

echo "=== recycle neighbor (remove + add) ==="
curl -s -X POST "http://127.0.0.1:9179/api/neighbors/remove" \
  -H 'Content-Type: application/json' \
  -d "{\"vrf\":\"$VRF\",\"address\":\"$PEER\"}" | head -c 200; echo

sleep 2

curl -s -X POST "http://127.0.0.1:9179/api/neighbors/add" \
  -H 'Content-Type: application/json' \
  -d "{
    \"vrf\":\"$VRF\",
    \"address\":\"$PEER\",
    \"remote_as\":63199,
    \"role\":\"downstream\",
    \"local_address\":\"$SPOOF\",
    \"bind_interface\":\"iv204\"
  }" | head -c 300; echo

sleep 8

echo "=== status ==="
curl -s "$OP/api/bgp/neighbors" | python3 -c "
import json,sys
for r in json.load(sys.stdin):
  if r.get('vrf')=='$VRF' and r.get('neighbor_ip')=='$PEER':
    print(r)
"
curl -s http://127.0.0.1:9179/api/peers/freeze-status | python3 -c "
import json,sys
d=json.load(sys.stdin)
for p in d.get('downstream',[]):
  if p.get('vrf')=='$VRF':
    print(p)
"
ss -tnp state established 2>/dev/null | grep -E '153.204|152.204|1833' || true
