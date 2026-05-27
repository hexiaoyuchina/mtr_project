#!/usr/bin/env python3
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script
load_env_file(ROOT / "109" / "env")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(c, r"""
curl -sf http://127.0.0.1:8808/api/hop-rules | python3 -m json.tool | head -80
echo '--- global ---'
curl -sf http://127.0.0.1:8808/api/global | python3 -m json.tool
echo '--- fib 8.8.8.8 ---'
curl -sf 'http://127.0.0.1:9179/api/fib/routes?window=upstream&prefix=8.8.8.8' | python3 -m json.tool
echo '--- te process / iptables ---'
ps aux | grep -E 'te_rewrite|nfqueue' | grep -v grep || true
iptables -t mangle -L FORWARD -n -v 2>/dev/null | head -6
iptables -t mangle -L OUTPUT -n -v 2>/dev/null | head -4
""", 30)
print(out)
c.close()
