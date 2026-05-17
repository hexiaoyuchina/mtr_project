#!/bin/bash
python3 -c "import sqlite3; c=sqlite3.connect('/root/mtr_op/data.db'); print('settings', c.execute('select * from arp_spoof_settings').fetchall()); print('targets', c.execute('select id,spoof_gateway_ip,egress_iface,enabled from arp_spoof_targets').fetchall())"
echo "=== neigh 249 ==="
ip neigh show 139.159.43.249
echo "=== mac enp59 ==="
ip link show enp59s0f0np0 | grep link
tail -20 /tmp/arp_spoof_daemon.log 2>/dev/null || true
