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
curl -sf 'http://127.0.0.1:8808/api/bgp/learned-routes/filter-options' | python3 -c "import sys,json; j=json.load(sys.stdin); print('fib_summary', j.get('fib_summary'))"
echo '--- fib page1 ---'
curl -sf 'http://127.0.0.1:8808/api/bgp/fib-routes?route_window=upstream&page=1&page_size=3' | python3 -m json.tool | head -40
echo '--- fib prefix ---'
curl -sf 'http://127.0.0.1:8808/api/bgp/fib-routes?route_window=upstream&prefix=8.8.8.8' | python3 -m json.tool | head -25
""", 60)
print(out)
c.close()
