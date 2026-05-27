#!/usr/bin/env python3
import os, sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script
load_env_file(ROOT / "109" / "env")
load_env_file(ROOT / "109" / "env.example")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(c, r"""
echo "=== FIB counts ==="
curl -sf 'http://127.0.0.1:9179/api/fib/routes/count?window=upstream'
echo
curl -sf 'http://127.0.0.1:9179/api/fib/routes/count?window=downstream'
echo
echo "=== neighbors ==="
curl -sf 'http://127.0.0.1:9179/api/neighbors' | python3 -m json.tool
echo "=== export state (208) ==="
redis-cli GET 'export:cnt:tx:tx:vbgp13915943249:139.159.43.208:139.159.43.249'
echo
echo "=== pipeline consistency ==="
curl -sf 'http://127.0.0.1:9179/api/pipeline/consistency' | python3 -m json.tool | head -80
echo "=== recent export logs ==="
journalctl -u bgp-agent --since '2 hours ago' --no-pager | grep -iE 'export|upstream stream|AddPath|208' | tail -25
echo "=== gobgp 208 adj-out sample ==="
curl -sf 'http://127.0.0.1:9179/api/tx/routes/count?vrf=vbgp13915943249' 2>/dev/null; echo
""", 90)
print(out)
c.close()
