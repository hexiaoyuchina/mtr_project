#!/bin/bash
echo "=== arp spoof process ==="
pgrep -af te_rewrite || true
echo "=== arp targets 249 ==="
python3 -c "
import sqlite3
c=sqlite3.connect('/root/mtr_op/data.db')
for r in c.execute('select id,spoof_gateway_ip,egress_iface,enabled,satellite_vrf from arp_spoof_targets where spoof_gateway_ip like \"%249%\"'):
    print(r)
" 2>/dev/null || true
echo "=== who has 249 on wire ==="
ip neigh show 139.159.43.249
echo "=== tcpdump passive 20s port 179/1830 ==="
timeout 20 tcpdump -ni enp59s0f0np0 -c 40 'tcp port 179 or tcp port 1830' 2>&1 | head -50
