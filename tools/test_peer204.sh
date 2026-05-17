#!/bin/bash
A=http://127.0.0.1:9179
curl -sf -X POST -H 'Content-Type: application/json' -d \
  '{"address":"139.159.43.204","remote_as":63199,"role":"downstream","vrf":"vbgp13915943249","local_address":"139.159.43.249","bind_interface":"iv249","passive_mode":true}'
echo ""
sleep 12
curl -sf $A/api/neighbors
echo ""
ss -tn | grep 204 || true
ss -tn | grep 1830 || true
timeout 8 tcpdump -ni enp59s0f0np0 -c 12 'tcp port 1830' 2>&1
