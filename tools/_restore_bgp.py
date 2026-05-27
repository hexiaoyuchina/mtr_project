#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script
load_env_file(ROOT / "109" / "env")
load_env_file(ROOT / "109" / "env.example")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(c, r"""
curl -sf -X POST http://127.0.0.1:8808/api/bgp/restore-agent -H 'Content-Type: application/json' -d '{}' | head -c 1500
echo
sleep 45
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -m json.tool
""", 180)
print(out)
c.close()
