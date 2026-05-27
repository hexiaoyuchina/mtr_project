#!/usr/bin/env python3
"""109: is table 2103 still needed for 207->249 without it?"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script
load_env_file(ROOT / "109" / "env")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(c, r"""
echo '=== rules 40-55 + 30449 ==='
ip -4 rule list | grep -E 'pref (4[0-9]|5[0-5])|30449|2103' || true
echo
echo '=== table 2103 ==='
ip -4 route show table 2103 || true
echo
echo '=== main uplink 43.0/24 ==='
ip -4 route show | grep -E '43\.(0|207|249)' | head -8
echo
echo '=== route get 249 from 207 (current) ==='
ip route get 139.159.43.249 from 139.159.43.207 2>&1
echo
echo '=== main-only: del rules 45/50 temporarily? NO - show what main gives ==='
ip route get 139.159.43.249 from 139.159.43.207 oif enp59s0f0np0 2>&1 || true
echo
echo '=== rules that match FROM 207 (all prefs) ==='
ip -4 rule list | grep '139.159.43.207' || true
echo
echo '=== iv249 / satellite rules for 249 ==='
ip -4 rule list | grep -E '249|30449' | head -15
ip -4 route show table 30449 2>/dev/null | head -5 || true
echo
echo '=== BGP :179 ==='
ss -tn sport = :179 or dport = :179 2>/dev/null | grep 43. | head -8 || ss -tn | grep ':179' | head -8
""", 25)
print(out)
c.close()
