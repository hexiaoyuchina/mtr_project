#!/usr/bin/env python3
"""Temporarily remove pref 45/50, compare route get, restore."""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script
load_env_file(ROOT / "109" / "env")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(c, r"""
set -e
echo '--- BEFORE (with policy) ---'
ip route get 139.159.43.249 from 139.159.43.207 2>&1
ip route get 139.159.43.245 from 139.159.43.207 2>&1

ip -4 rule del pref 45 2>/dev/null || true
ip -4 rule del pref 50 2>/dev/null || true
echo
echo '--- WITHOUT pref 45/50 (main only) ---'
ip route get 139.159.43.249 from 139.159.43.207 2>&1
ip route get 139.159.43.245 from 139.159.43.207 2>&1
ip route get 139.159.43.208 from 139.159.43.207 2>&1

# restore
ip -4 rule add pref 45 from 139.159.43.207 to 139.159.43.249 lookup 2103
ip -4 rule add pref 50 from 139.159.43.207 lookup 2103
echo
echo '--- RESTORED ---'
ip route get 139.159.43.249 from 139.159.43.207 2>&1
""", 20)
print(out)
c.close()
