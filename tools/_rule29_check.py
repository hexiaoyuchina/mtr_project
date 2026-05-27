#!/usr/bin/env python3
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script
load_env_file(ROOT / "109" / "env")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(c, """
ip -4 rule list | awk '/^29:|^30:/'
echo '--- 2111 ---'
ip route show table 2111
echo '--- route get return ---'
ip route get 139.159.105.94 from 8.8.8.8 iif enp59s0f0np0 2>&1 | head -2
""", 15)
print(out)
c.close()
