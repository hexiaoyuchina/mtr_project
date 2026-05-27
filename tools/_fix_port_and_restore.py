#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script
load_env_file(ROOT / "109" / "env")
load_env_file(ROOT / "109" / "env.example")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(c, r"""
echo "=== port 51830 ==="
ss -ltnp | grep 51830 || true
fuser -k 51830/tcp 2>/dev/null || true
sleep 2
systemctl restart bgp-agent
sleep 90
curl -sf -X POST http://127.0.0.1:8808/api/bgp/restore-agent -H 'Content-Type: application/json' -d '{}'
echo
sleep 60
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -m json.tool | head -60
journalctl -u bgp-agent -n 15 --no-pager | grep -E 'fatal|51830|208|export upstream'
""", 240)
print(out)
c.close()
