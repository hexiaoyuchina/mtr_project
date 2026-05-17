#!/bin/bash
A=http://127.0.0.1:9179
echo "=== before ==="
curl -sf --max-time 5 $A/api/neighbors; echo
ss -tn | grep 249 || echo "no tcp 249"
echo "=== rr/config ==="
curl -sf --max-time 10 -X POST -H 'Content-Type: application/json' \
  -d '{"address":"139.159.43.249","remote_as":63199,"local_address":"139.159.43.207"}' \
  $A/api/rr/config; echo
sleep 8
echo "=== after ==="
curl -sf --max-time 5 $A/api/neighbors; echo
curl -sf --max-time 5 $A/api/status; echo
ss -tn | grep 249 || echo "no tcp 249"
echo "=== route ==="
ip route get 139.159.43.249 from 139.159.43.207
echo "=== tcpdump 8s to RR ==="
(timeout 8 tcpdump -ni enp59s0f0np0 -c 12 'host 139.159.43.249 and port 179' 2>&1) &
TP=$!
sleep 8
wait $TP 2>/dev/null
journalctl -u bgp-agent --since '3 min ago' --no-pager | grep -iE '249|Peer Up|Peer Down|error' | tail -12
