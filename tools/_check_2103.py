#!/usr/bin/env python3
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script
load_env_file(ROOT / "109" / "env")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(c, """
for d in 245 247 249; do
  echo "=== to 43.$d from 207 ==="
  ip route get 139.159.43.$d from 139.159.43.207 2>&1
done
echo '=== ss bgp 179 ==='
ss -tn | grep ':179' | head -10
""", 20)
print(out)
c.close()
