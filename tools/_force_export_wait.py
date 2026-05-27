#!/usr/bin/env python3
"""Wait for 208 ESTABLISHED + unfreeze, then force full re-advertise."""
import os, sys, time, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script

load_env_file(ROOT / "109" / "env")
load_env_file(ROOT / "109" / "env.example")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))

def run(cmd, timeout=60):
    _, out = run_script(c, cmd, timeout)
    return out.strip()

for i in range(36):
    nb = run(r"""curl -sf 'http://127.0.0.1:9179/api/neighbors' | python3 -c "
import sys,json
for p in json.load(sys.stdin).get('neighbors',[]):
    if p.get('address')=='139.159.43.208':
        print(p.get('state',''), p.get('pfx_rcd',0))
" """, 20)
    print(f"try {i+1}: 208 -> {nb}")
    if "ESTABLISHED" in nb.upper():
        break
    time.sleep(5)
else:
    print("208 not established after 3min")
    c.close()
    sys.exit(1)

time.sleep(3)
print("=== TX agent status ===")
print(run(r"""curl -sf 'http://127.0.0.1:9179/api/tx/agents' 2>/dev/null || echo no_tx_api""", 15))

print("=== force reconcile ===")
print(run(r"""curl -sf -X POST 'http://127.0.0.1:9179/api/export/reconcile?force=1'""", 15))

print("waiting 120s for stream export...")
time.sleep(120)

print("=== export logs ===")
print(run(r"""journalctl -u bgp-agent -n 30 --no-pager | grep -E 'export|frozen|208'""", 30))

print("=== export cnt ===")
print(run(r"""redis-cli GET 'export:cnt:tx:tx:vbgp13915943249:139.159.43.208:139.159.43.249'""", 15))

print("=== FIB count ===")
print(run(r"""curl -sf 'http://127.0.0.1:9179/api/fib/routes/count?window=upstream'""", 15))

c.close()
