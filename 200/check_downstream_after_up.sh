#!/bin/bash
echo "=== 200 ==="
ss -tnp | grep -E '152.204|1833|1790' || true
curl -sf http://127.0.0.1:9179/api/peers/freeze-status | python3 -m json.tool | head -25
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle \
  -H 'Content-Type: application/json' \
  -d '{"address":"10.133.152.204","vrf":"vbgp10133153204","enabled":false}'
sleep 2
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle \
  -H 'Content-Type: application/json' \
  -d '{"address":"10.133.152.204","vrf":"vbgp10133153204","enabled":true}'
sleep 25
ss -tnp | grep 152.204 || true
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='10.133.152.204':
    print(n.get('vrf'), n.get('state'), n.get('pfx_rcd'))
"
