#!/usr/bin/env python3
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script
load_env_file(ROOT / "109" / "env")
load_env_file(ROOT / "109" / "env.example")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(c, r"""
systemctl is-active bgp-agent
journalctl -u bgp-agent -n 50 --no-pager
curl -sf http://127.0.0.1:9179/health || echo health_fail
curl -sf http://127.0.0.1:9179/api/neighbors || echo neighbors_fail
""", 60)
print(out)
c.close()
