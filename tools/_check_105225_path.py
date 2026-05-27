#!/usr/bin/env python3
"""109 上核对 105.225 -> 8.8.8.8 去回程。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script

load_env_file(ROOT / "109" / "env")
load_env_file(ROOT / "109" / "env.example")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(
    c,
    r"""
echo "=== forward 8.8.8.8 from 105.225 iif eno1np0 ==="
ip route get 8.8.8.8 from 139.159.105.225 iif eno1np0 2>&1
echo
echo "=== return 105.225 from 8.8.8.8 iif uplink ==="
ip route get 139.159.105.225 from 8.8.8.8 iif enp59s0f0np0 2>&1
echo
echo "=== neigh 105.225 on eno1np0 ==="
ip neigh show dev eno1np0 | grep -E '105\.22[45]' || echo "(no permanent neigh for 105.225)"
echo
echo "=== global / hop-rules ==="
curl -sf http://127.0.0.1:8808/api/global 2>/dev/null | python3 -m json.tool 2>/dev/null | head -15
curl -sf http://127.0.0.1:8808/api/hop-rules 2>/dev/null | python3 -c "
import json,sys
for x in json.load(sys.stdin):
    if x.get('enabled'):
        print(x.get('match_cidr'), '->', x.get('forged_src'), 'prio', x.get('priority'))
" 2>/dev/null
""",
    45,
)
print(out)
c.close()
