#!/usr/bin/env python3
"""109：清空 208 会话 export state 并全量 re-advertise upstream_fib。"""
import os, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script

TARGET = "tx:vbgp13915943249:139.159.43.208:139.159.43.249"

load_env_file(ROOT / "109" / "env")
load_env_file(ROOT / "109" / "env.example")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
script = f"""
set -e
TARGET={TARGET!r}
PAT="export:tx:${{TARGET}}:*"
echo "=== export cnt before ==="
redis-cli GET "export:cnt:tx:${{TARGET}}"
echo "=== clearing export keys (scan) ==="
n=0
while IFS= read -r k; do
  redis-cli DEL "$k" >/dev/null
  n=$((n+1))
  if (( n % 50000 == 0 )); then echo "deleted $n"; fi
done < <(redis-cli --scan --pattern "$PAT")
redis-cli DEL "export:cnt:tx:${{TARGET}}" >/dev/null || true
echo "deleted_prefix_keys=$n"
echo "=== trigger export reconcile ==="
curl -sf -X POST 'http://127.0.0.1:9179/api/export/reconcile'
echo
echo "waiting 90s..."
sleep 90
echo "=== after ==="
redis-cli GET "export:cnt:tx:${{TARGET}}"
journalctl -u bgp-agent --since '3 min ago' --no-pager | grep 'export upstream stream' | tail -3
"""
_, out = run_script(c, script, timeout=600)
print(out)
c.close()
