#!/bin/bash
# 249/32 在 iv249 上会导致本机把真 RR 当本地地址，207 无法对外建连
ip addr del 139.159.43.249/32 dev iv249 2>/dev/null || true
ip route flush cache
echo "route:" $(ip route get 139.159.43.249 from 139.159.43.207)
(timeout 6 tcpdump -ni enp59s0f0np0 -c 5 'src 139.159.43.207 and dst 139.159.43.249 and port 179' 2>&1) &
TP=$!
sleep 1
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"address":"139.159.43.249","remote_as":63199,"local_address":"139.159.43.207"}' \
  http://127.0.0.1:9179/api/rr/config
echo ""
sleep 12
wait $TP 2>/dev/null || true
curl -sf http://127.0.0.1:9179/api/neighbors; echo
ss -tn | grep 249 || true
