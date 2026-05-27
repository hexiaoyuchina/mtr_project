#!/usr/bin/env python3
"""109 上核对 105.94 -> 8.8.8.8 去回程策略路由。"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script
load_env_file(ROOT / "109" / "env")
load_env_file(ROOT / "109" / "env.example")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(c, r"""
echo "=== 去程: 8.8.8.8 from 105.94 iif eno1np0 ==="
ip route get 8.8.8.8 from 139.159.105.94 iif eno1np0 2>&1
echo
echo "=== 回程: 105.94 from 8.8.8.8 iif enp59s0f0np0 ==="
ip route get 139.159.105.94 from 8.8.8.8 iif enp59s0f0np0 2>&1
echo
echo "=== 回程: 105.94 (无 from) ==="
ip route get 139.159.105.94 2>&1
echo
echo "=== ip rule (29/30/31) ==="
ip -4 rule list | grep -E 'pref (29|30|31)|105\.9|2110|2111' || ip -4 rule list | head -25
echo
echo "=== table 2110 default + 8.8.8 sample ==="
ip route show table 2110 | head -5
ip route show table 2110 | grep -E '^default|8\.8\.8' | head -5
echo
echo "=== table 2111 ==="
ip route show table 2111
echo
echo "=== neigh 105.94 on eno1np0 ==="
ip neigh show dev eno1np0 | grep 105.94 || echo "(no neigh)"
echo
echo "=== FIB upstream count ==="
curl -sf 'http://127.0.0.1:9179/api/fib/routes/count?window=upstream' 2>/dev/null || echo agent_n/a
echo
echo "=== BGP 208 session ==="
curl -sf 'http://127.0.0.1:9179/api/neighbors' 2>/dev/null | python3 -c "
import sys,json
for p in json.load(sys.stdin).get('neighbors') or []:
    if p.get('address')=='139.159.43.208':
        print(p)
" 2>/dev/null || echo n/a
""", 45)
print(out)
c.close()
