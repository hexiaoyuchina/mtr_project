#!/bin/bash
set -e
cd /root/mtr_op
./venv/bin/python3 /root/mtr_op/remote-fix-arp-db.py
curl -sf -X PUT http://127.0.0.1:8808/api/arp-spoof/settings \
  -H 'Content-Type: application/json' -d '{"arp_spoof_enabled":true}'
echo
curl -sf -X POST http://127.0.0.1:8808/api/bgp/ipvlan-satellites/reconcile
echo
./venv/bin/python3 <<'PY'
import json, urllib.request, time
body={
  "vrf":"vbgp10133152233","neighbor_ip":"10.133.152.204","remote_as":63199,
  "role":"downstream","source_ip":"10.133.152.233",
  "bgp_local_as":63199,"bgp_router_id":"10.133.152.233",
  "create_kernel_vrf_if_missing": True,
}
r=urllib.request.Request('http://127.0.0.1:8808/api/bgp/neighbors',
  json.dumps(body).encode(), method='POST',
  headers={'Content-Type':'application/json'})
try:
  print(urllib.request.urlopen(r,timeout=90).read().decode())
except urllib.error.HTTPError as e:
  print(e.read().decode())
time.sleep(4)
print(urllib.request.urlopen('http://127.0.0.1:9179/api/peers/freeze-status').read().decode())
PY
ss -tnp state established 2>/dev/null | grep -E '152.233|152.204' || true
