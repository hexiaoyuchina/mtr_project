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
ss -ltnp | grep -E '51830|1790|9179' || true
journalctl -u bgp-agent -n 20 --no-pager
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -m json.tool 2>/dev/null | head -40
""", 30)
print(out)
c.close()
