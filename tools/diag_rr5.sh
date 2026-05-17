#!/bin/bash
# delete and re-add peer to force connect
curl -sf -X DELETE http://127.0.0.1:9179/api/rr/config 2>/dev/null || true
sleep 1
timeout 10 tcpdump -ni enp59s0f0np0 'host 139.159.43.207 and host 139.159.43.249' 2>&1 &
TP=$!
sleep 1
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"address":"139.159.43.249","remote_as":63199,"local_address":"139.159.43.207"}' \
  http://127.0.0.1:9179/api/rr/config
echo
sleep 5
kill $TP 2>/dev/null
wait $TP 2>/dev/null
echo '--- ss ---'
ss -tn 'sport = :179 or dport = :179' | grep -E '207|249' || ss -tn | grep -E '207|249' || echo none
echo '--- conntrack ---'
conntrack -L 2>/dev/null | grep '249.*179\|207.*179' | head -5 || true
echo '--- nft output ---'
nft list ruleset 2>/dev/null | grep -i output | head -20
