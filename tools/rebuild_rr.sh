#!/bin/bash
cd /root/mtr_op/bgp_agent
export PATH=/usr/local/go/bin:$PATH
go build -o bgp_agent .
systemctl restart bgp-agent
sleep 4
curl -sf http://127.0.0.1:9179/health; echo
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"address":"139.159.43.249","remote_as":63199,"local_address":"139.159.43.207"}' \
  http://127.0.0.1:9179/api/rr/config
echo ""
sleep 15
curl -sf http://127.0.0.1:9179/api/neighbors; echo
ss -tn | grep '207.*249\|249.*207' || ss -tn | grep 249 || true
