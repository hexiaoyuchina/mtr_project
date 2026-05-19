#!/usr/bin/env python3
"""只读：109 控制面 + 尽量查 ROS(249) 是否收到 139.159.105.92/30。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit(2)

DEPLOY = Path(__file__).resolve().parent
PREFIX = "139.159.105.92"
RR = "139.159.43.249"
PEER_207 = "139.159.43.207"


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        if name == "env":
            break


SCRIPT = r"""
set +e
RR=139.159.43.249
PEER207=139.159.43.207
PFX=139.159.105.92

echo "=== A) 109 Agent/OP ==="
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
d=json.load(sys.stdin)
for n in d.get('neighbors') or []:
    if n.get('address')=='139.159.43.249':
        print('RR peer:', json.dumps(n, ensure_ascii=False))
"
curl -sf "http://127.0.0.1:9179/api/rib/routes/count?window=downstream&vrf=vbgp13915943249&neighbor_ip=139.159.43.208"
echo ""
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors/gobgp-rr/139.159.43.249/advertise/status"
echo ""

echo ""
echo "=== B) gobgp RX adj-out / global (是否向 249 发出) ==="
if command -v gobgp >/dev/null 2>&1; then
  echo "--- neighbor state ---"
  gobgp -p 50052 neighbor $RR 2>/dev/null | head -15
  echo "--- adj-out grep $PFX ---"
  gobgp -p 50052 neighbor $RR adj-out 2>/dev/null | grep -F "$PFX" || echo "(adj-out 无 $PFX)"
  echo "--- adj-out line count ---"
  gobgp -p 50052 neighbor $RR adj-out 2>/dev/null | wc -l
  echo "--- global rib $PFX/30 ---"
  gobgp -p 50052 global rib -a ipv4 ${PFX}/30 2>&1 | head -10
else
  echo "gobgp CLI not installed"
fi

echo ""
echo "=== C) journal rr-aggregate (7d) ==="
journalctl -u bgp-agent --since '7 days ago' --no-pager 2>/dev/null | grep -iE 'rr-aggregate|249-advertise|105\.92' | tail -12

echo ""
echo "=== D) 从 109 SSH 到 ROS $RR (admin) ==="
ROS_PW="${MTR_ROS_SSH_PASSWORD:-}"
if [ -z "$ROS_PW" ]; then
  echo "skip: MTR_ROS_SSH_PASSWORD 未设置（可在 109 环境变量或本机 109/env 配置）"
else
  sshpass -p "$ROS_PW" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 admin@$RR \
    "/routing bgp peer print stats where remote-address=$PEER207" 2>&1 | head -20
  echo "---"
  sshpass -p "$ROS_PW" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 admin@$RR \
    "/routing route print where dst-address~\"$PFX\"" 2>&1 | head -25
  echo "---"
  sshpass -p "$ROS_PW" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 admin@$RR \
    "/routing bgp route print where dst-address~\"$PFX\"" 2>&1 | head -25
fi
echo "=== D2) 109 -> ROS SSH key probe (no password) ==="
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=6 admin@$RR \
  "/routing route print where dst-address~\"$PFX\"" 2>&1 | head -15 || echo "key_ssh_failed"
"""


def main() -> None:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        sys.exit(2)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ.get("MTR_OP_HOST", "101.89.68.109"),
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=pw,
        timeout=30,
    )
    i, o, e = c.exec_command("bash -se", timeout=120)
    i.write(SCRIPT)
    i.channel.shutdown_write()
    print(o.read().decode(errors="replace") + e.read().decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
