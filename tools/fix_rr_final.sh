#!/bin/bash
ip neigh replace 139.159.43.249 lladdr 00:50:56:9c:ac:ed dev enp59s0f0np0 2>/dev/null || true
ip route replace 139.159.43.249/32 dev enp59s0f0np0
sysctl -w net.ipv4.ip_nonlocal_bind=1
ip addr del 139.159.43.249/32 dev iv249 2>/dev/null || true
systemctl restart bgp-agent
sleep 5
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"address":"139.159.43.249","remote_as":63199,"local_address":"139.159.43.207"}' \
  http://127.0.0.1:9179/api/rr/config
echo ""
sleep 25
curl -sf http://127.0.0.1:9179/api/neighbors; echo
curl -sf http://127.0.0.1:9179/api/status; echo
ss -tn | grep 249
