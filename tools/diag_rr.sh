#!/bin/bash
set -x
journalctl -u bgp-agent -n 80 --no-pager
echo '--- ss ---'
ss -tnp | grep -E '179|249|207' || true
echo '--- route ---'
ip route get 139.159.43.249 from 139.159.43.207
echo '--- nc ---'
nc -vz -w3 -s 139.159.43.207 139.159.43.249 179 2>&1 || true
echo '--- iv249 ---'
ip addr show iv249 2>/dev/null || true
ip rule | grep 207 || true
echo '--- gobgp peer dump via grpc? ---'
curl -sf http://127.0.0.1:9179/api/status; echo
