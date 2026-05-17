#!/bin/bash
systemctl restart bgp-agent
sleep 5
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"address":"139.159.43.249","remote_as":63199,"local_address":"139.159.43.207"}' \
  http://127.0.0.1:9179/api/rr/config
echo ""
sleep 12
echo route: $(ip route get 139.159.43.249 from 139.159.43.207)
(timeout 6 tcpdump -ni enp59s0f0np0 -c 8 'host 139.159.43.249 and tcp port 179' 2>&1) &
sleep 6
curl -sf http://127.0.0.1:9179/api/neighbors; echo
ss -tn | grep 249 || echo no249
journalctl -u bgp-agent -n 20 --no-pager | grep -iE 'Peer Up|249|error|fail'
