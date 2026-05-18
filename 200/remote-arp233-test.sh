#!/bin/bash
set -e
DB=/root/mtr_op/data.db
IP=10.133.152.233
VRF=vbgp10133152233
IF=ens192

sqlite3 "$DB" "PRAGMA table_info(arp_spoof_targets);"

if ! sqlite3 "$DB" "SELECT 1 FROM arp_spoof_targets WHERE spoof_gateway_ip='$IP';" | grep -q 1; then
  sqlite3 "$DB" "INSERT INTO arp_spoof_targets (spoof_gateway_ip, satellite_vrf, egress_iface, enabled, policy_mode, policy_cidrs, note, created_at) VALUES ('$IP','$VRF','$IF',1,'gateway_only','','lab233-test',datetime('now'));"
else
  sqlite3 "$DB" "UPDATE arp_spoof_targets SET satellite_vrf='$VRF', egress_iface='$IF', enabled=1, note='lab233-test' WHERE spoof_gateway_ip='$IP';"
fi
sqlite3 "$DB" "SELECT id,spoof_gateway_ip,satellite_vrf,egress_iface FROM arp_spoof_targets WHERE spoof_gateway_ip='$IP';"

curl -sf -X PUT http://127.0.0.1:8808/api/arp-spoof/settings -H 'Content-Type: application/json' -d '{"arp_spoof_enabled":true}'
echo settings_ok

curl -sf -X POST http://127.0.0.1:8808/api/arp-spoof/satellite-vrfs/reconcile
echo
curl -sf -X POST http://127.0.0.1:8808/api/bgp/ipvlan-satellites/reconcile
echo
sleep 2
ip addr show "$IF" | grep "$IP" || echo "no $IP on $IF"
ip link show "$VRF" 2>/dev/null | head -2 || true
