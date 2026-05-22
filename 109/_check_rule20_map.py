#!/usr/bin/env python3
"""检查 109 上规则 #20 与 TE 映射是否一致。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import paramiko  # noqa: E402

for line in (ROOT / "109" / "env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

host = os.environ["MTR_OP_HOST"]
pw = os.environ["MTR_OP_SSH_PASSWORD"]

script = r"""
set -e
echo "=== API rule 20 ==="
curl -s http://127.0.0.1:8808/api/hop-rules | python3 -c "
import json,sys
for r in json.load(sys.stdin):
    if r.get('id')==20:
        print(r)
        break
else:
    print('rule 20 not found')
"
echo "=== map file (142.251) ==="
grep 142.251 /tmp/mtr_te_map.env || echo '(no 142.251 in map)'
echo "=== te_rewrite log (last 10) ==="
tail -10 /tmp/te_rewrite_nfqueue.log 2>/dev/null || true
echo "=== te_rewrite pid ==="
pgrep -af te_rewrite_nfqueue || true
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
_, stdout, stderr = c.exec_command(script, timeout=30)
print(stdout.read().decode("utf-8", errors="replace"))
err = stderr.read().decode("utf-8", errors="replace")
if err.strip():
    print(err, file=sys.stderr)
c.close()
