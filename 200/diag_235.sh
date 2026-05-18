#!/bin/bash
set -x
for V in vbgp10133152233 vbgp10133152235; do
  echo "==== routes $V ===="
  ip route show vrf "$V"
  echo "==== master addrs $V ===="
  ip -br addr show master "$V" 2>/dev/null || true
  SPOOF=$(ip -br addr show master "$V" 2>/dev/null | awk '{print $3}' | head -1 | cut -d/ -f1)
  echo "spoof=$SPOOF"
  if [ -n "$SPOOF" ]; then
    ip vrf exec "$V" ping -c1 -W2 -I "$SPOOF" 10.133.152.204 || true
  fi
done
echo "==== listen bgp_agent ===="
ss -tlnp | grep bgp_agent || true
echo "==== tcp 235 233 ===="
ss -tnp state established | grep -E '235|233' || true
echo "==== meta from sqlite ===="
sqlite3 /root/mtr_op/data.db "SELECT vrf, neighbor_ip, source_ip FROM bgp_neighbor_meta WHERE vrf LIKE 'vbgp%';" 2>/dev/null || true
