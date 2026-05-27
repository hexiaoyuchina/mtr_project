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
echo "=== 208 session ==="
curl -sf 'http://127.0.0.1:9179/api/neighbors' | python3 -c "import sys,json; [print(p) for p in json.load(sys.stdin).get('neighbors',[]) if p.get('address')=='139.159.43.208']"
echo "=== force export reconcile ==="
curl -sf -X POST 'http://127.0.0.1:9179/api/export/reconcile?force=1'
echo
""", 30)
print(out)
c.close()
