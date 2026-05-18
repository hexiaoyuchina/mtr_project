#!/bin/bash
set -e
curl -s -X POST http://127.0.0.1:9179/api/neighbors/add -H 'Content-Type: application/json' -d '{
  "vrf": "vbgp10133152233",
  "address": "10.133.152.204",
  "remote_as": 63199,
  "local_address": "10.133.152.233",
  "bind_interface": "iv233@ens192",
  "role": "downstream"
}'
echo
sleep 5
echo "=== neighbors ==="
curl -s http://127.0.0.1:9179/api/neighbors
echo
echo "=== freeze ==="
curl -s http://127.0.0.1:9179/api/peers/freeze-status
echo
echo "=== tcp ==="
ss -tnp state established 2>/dev/null | grep -E '152.233|152.204' || true

cd /root/mtr_op
./venv/bin/python3 <<'PY'
import sys
sys.path.insert(0, "/root/mtr_op")
from pathlib import Path
from app import storage
conn = storage.connect(Path("/root/mtr_op/data.db"))
storage.set_bgp_neighbor_meta(
    conn, "vbgp10133152233", "10.133.152.204", "downstream", "", update_source="10.133.152.233"
)
conn.close()
print("meta_ok")
PY
