#!/bin/bash
ip vrf exec vbgp10133153204 ping -c2 -W2 -I 10.133.153.204 10.133.152.204
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle \
  -H 'Content-Type: application/json' \
  -d '{"address":"10.133.152.204","vrf":"vbgp10133153204","enabled":false}'
sleep 3
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle \
  -H 'Content-Type: application/json' \
  -d '{"address":"10.133.152.204","vrf":"vbgp10133153204","enabled":true}'
sleep 25
ss -tnp | grep 152.204
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('vrf')=='vbgp10133153204':
    print(n)
"
