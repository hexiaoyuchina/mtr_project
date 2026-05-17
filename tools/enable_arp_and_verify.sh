#!/bin/bash
set -e
python3 <<'PY'
import sqlite3
c=sqlite3.connect('/root/mtr_op/data.db')
c.execute('UPDATE arp_spoof_settings SET arp_spoof_enabled=1 WHERE id=1')
c.commit()
print('arp_spoof_enabled=1')
PY
pkill -f arp_spoof_daemon.py 2>/dev/null || true
sleep 1
nohup python3 /root/mtr_op/arp_spoof_daemon.py --op-db /root/mtr_op/data.db --verbose >>/tmp/arp_spoof_daemon.log 2>&1 &
sleep 12
echo "=== neigh 249 after GARP ==="
ip neigh show 139.159.43.249
echo "=== SYN test 208 ==="
ip neigh show 139.159.43.208 dev iv249
timeout 5 tcpdump -ni enp59s0f0np0 -c 8 'host 139.159.43.208 and tcp port 179' 2>&1 &
TP=$!
ip vrf exec vbgp13915943249 timeout 3 bash -c 'echo | nc -w2 -s 139.159.43.249 139.159.43.208 179' 2>&1 || true
wait $TP 2>/dev/null || true
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -m json.tool 2>/dev/null | head -40
