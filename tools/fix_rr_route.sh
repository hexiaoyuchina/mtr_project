#!/bin/bash
# 主表到真 RR（249）的可达，且不在 iv249 上配置 249/32
ip neigh replace 139.159.43.249 lladdr 00:50:56:9c:ac:ed dev enp59s0f0np0 nud reachable 2>/dev/null || \
  ip neigh replace 139.159.43.249 lladdr 00:50:56:9c:ac:ed dev enp59s0f0np0
ip route replace 139.159.43.249/32 dev enp59s0f0np0
sysctl -w net.ipv4.ip_nonlocal_bind=1
ip route flush cache
echo route: $(ip route get 139.159.43.249 from 139.159.43.207)
timeout 3 nc -v -w2 -s 139.159.43.207 139.159.43.249 179 2>&1 || true
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"address":"139.159.43.249","remote_as":63199,"local_address":"139.159.43.207"}' \
  http://127.0.0.1:9179/api/rr/config; echo
sleep 15
curl -sf http://127.0.0.1:9179/api/neighbors; echo
ss -tn | grep '207.*249\|249.*207' || true
