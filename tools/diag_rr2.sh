#!/bin/bash
nft list ruleset 2>/dev/null | grep -A3 -B1 '249\|179\|1830' | head -80
echo '--- ip addr 207 ---'
ip addr | grep -E '139.159.43.(207|249)' 
echo '--- tcpdump 3s while trigger ---'
timeout 3 tcpdump -ni enp59s0f0np0 'host 139.159.43.249 and port 179' 2>/dev/null &
TP=$!
sleep 1
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"address":"139.159.43.249","remote_as":63199,"local_address":"139.159.43.207"}' \
  http://127.0.0.1:9179/api/rr/config >/dev/null
sleep 4
wait $TP 2>/dev/null || true
echo '--- ss after ---'
ss -tnp | grep 249 || echo 'no 249 tcp'
journalctl -u bgp-agent --since '1 min ago' --no-pager | tail -20
