#!/usr/bin/env python3
"""在 109 的 mtr-op-downstream-transit.sh 末尾挂上 stealth（若尚未挂）。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script

load_env_file(ROOT / "109" / "env")
load_env_file(ROOT / "109" / "env.example")
stealth = os.environ.get(
    "MTR_INBOUND_STEALTH_PERSIST",
    "/usr/local/sbin/mtr-op-inbound-trace-stealth.sh",
)
down = os.environ.get(
    "MTR_DOWNSTREAM_TRANSIT_PERSIST",
    "/usr/local/sbin/mtr-op-downstream-transit.sh",
)
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(
    c,
    f"""
set -e
DOWN={down!r}
STEALTH={stealth!r}
MARK='mtr-op-inbound-trace-stealth'
if [ ! -f "$DOWN" ]; then
  echo "skip: no $DOWN"
  exit 0
fi
if grep -q "$MARK" "$DOWN"; then
  echo "already hooked in $DOWN"
else
  printf '\n# %s\nSTEALTH=%s\n[ -x "$STEALTH" ] && "$STEALTH"\n' "$MARK" "$STEALTH" >> "$DOWN"
  echo "appended stealth hook to $DOWN"
fi
tail -5 "$DOWN"
""",
    30,
)
print(out)
c.close()
