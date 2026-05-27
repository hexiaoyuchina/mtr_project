#!/usr/bin/env python3
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script
load_env_file(ROOT / "109" / "env")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(c, r"""
echo '=== routes to 249 (all tables) ==='
ip -4 route show | grep 249 || true
ip -4 route show table all | grep 249 | head -20
echo
echo '=== route get 249 from 207 table main ==='
ip route get 139.159.43.249 from 139.159.43.207 table main 2>&1
echo
echo '=== route get 208 from 207 (no 2103 route) ==='
ip route get 139.159.43.208 from 139.159.43.207 2>&1
echo
echo '=== compare: 249 from 207 without policy? ==='
# simulate: only main lookup (bypass policy) not trivial on linux; show both /24 metrics
ip -4 route show 139.159.43.0/24
echo
ip -4 addr show iv249 2>/dev/null | head -4
ip -4 route show dev iv249 | head -6
""", 20)
print(out)
c.close()
