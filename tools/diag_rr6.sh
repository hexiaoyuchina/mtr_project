#!/bin/bash
curl -sf -X POST http://127.0.0.1:9179/api/rr/remove; echo
sleep 2
timeout 12 tcpdump -ni enp59s0f0np0 'host 139.159.43.207 and host 139.159.43.249' 2>&1 &
TP=$!
sleep 1
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"address":"139.159.43.249","remote_as":63199,"local_address":"139.159.43.207"}' \
  http://127.0.0.1:9179/api/rr/config; echo
sleep 8
kill $TP 2>/dev/null; wait $TP 2>/dev/null
ss -tn | grep 249 || echo 'no tcp 249'
curl -sf http://127.0.0.1:9179/api/neighbors; echo
journalctl -u bgp-agent --since '30 sec ago' --no-pager | grep -i '249\|peer\|fail\|error' | tail -15
