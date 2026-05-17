#!/bin/bash
ip addr | grep 249
echo "---"
(timeout 5 tcpdump -ni enp59s0f0np0 -c 6 'host 139.159.43.249 and port 179' 2>&1) &
TP=$!
sleep 1
timeout 3 nc -v -w2 -s 139.159.43.207 139.159.43.249 179 2>&1 || true
wait $TP
echo "---"
# 强制经主表到 249（若 249 被 l3mdev 视为本地）
ip route get 139.159.43.249 oif enp59s0f0np0 from 139.159.43.207 2>&1 || true
