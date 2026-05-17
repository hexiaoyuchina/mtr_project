#!/bin/bash
A=http://127.0.0.1:9179
curl -sf -X POST -H 'Content-Type: application/json' -d '{"address":"139.159.43.208","vrf":"vbgp13915943249"}' $A/api/neighbors/remove
curl -sf -X POST -H 'Content-Type: application/json' -d \
  '{"address":"139.159.43.204","remote_as":63199,"role":"downstream","vrf":"vbgp13915943249","local_address":"139.159.43.249","bind_interface":"iv249","passive_mode":false}'
echo ""
sleep 15
curl -sf $A/api/neighbors
echo ""
ss -tn | grep 204
timeout 6 tcpdump -ni enp59s0f0np0 -c 10 'host 139.159.43.204 and tcp port 179' 2>&1
