#!/usr/bin/env python3
"""一次性：109 上加 PREROUTING ttl-inc，去掉 FORWARD 重复项（不改仓库 apply 脚本）。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script

load_env_file(ROOT / "109" / "env")
load_env_file(ROOT / "109" / "env.example")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(
    c,
    r"""
set -e
UP=enp59s0f0np0
modprobe xt_TTL 2>/dev/null || true

echo "=== before ==="
iptables -t mangle -S PREROUTING | grep -i ttl || echo "(no prerouting ttl)"
iptables -t mangle -S FORWARD | grep -i ttl || echo "(no forward ttl)"

while iptables -t mangle -C FORWARD -i "$UP" -j TTL --ttl-inc 1 2>/dev/null; do
  iptables -t mangle -D FORWARD -i "$UP" -j TTL --ttl-inc 1
  echo "removed FORWARD ttl-inc"
done

if iptables -t mangle -C PREROUTING -i "$UP" -j TTL --ttl-inc 1 2>/dev/null; then
  echo "PREROUTING ttl-inc already present"
else
  iptables -t mangle -I PREROUTING 1 -i "$UP" -j TTL --ttl-inc 1
  echo "added PREROUTING ttl-inc -i $UP"
fi

echo "=== after ==="
iptables -t mangle -L PREROUTING -n -v | grep -E 'TTL|ttl' || true
iptables -t mangle -L FORWARD -n -v | grep -E 'TTL|ttl' || echo "(no forward ttl — ok)"
iptables -L OUTPUT -n -v | grep MTR_STEALTH || true
""",
    60,
)
print(out)
c.close()
