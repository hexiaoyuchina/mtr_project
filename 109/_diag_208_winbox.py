#!/usr/bin/env python3
"""下游 208：打开路由通告后 Winbox 连不上 — 只读诊断。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

DEPLOY_DIR = Path(__file__).resolve().parent


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY_DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        if name == "env":
            break


def run(c: paramiko.SSHClient, script: str, timeout: int = 120) -> str:
    i, o, e = c.exec_command("bash -se", timeout=timeout)
    i.write(script)
    i.channel.shutdown_write()
    return o.read().decode(errors="replace") + e.read().decode(errors="replace")


def main() -> int:
    load_env()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109")
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        print("需要 109/env 密码", file=sys.stderr)
        return 2

    script = r"""
set +e
PEER=139.159.43.208
VRF=vbgp13915943249
echo "=== 1) 下游邻居 / 通告开关 / 任务 ==="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors?vrf=$VRF" | python3 -c "
import json,sys
for n in json.load(sys.stdin):
    if n.get('neighbor_ip')=='139.159.43.208':
        print(json.dumps({k:n.get(k) for k in (
            'neighbor_ip','source_ip','session_state','enabled',
            'advertise_routes','routes_received','routes_sent','routes_cached',
            'store_received_routes')}, ensure_ascii=False, indent=2))
"
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors/$VRF/$PEER/advertise/status" 2>/dev/null
echo

echo "=== 2) Agent TX 208 ==="
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
d=json.load(sys.stdin)
for n in d.get('neighbors') or []:
    if n.get('address')=='139.159.43.208':
        print(json.dumps(n,ensure_ascii=False,indent=2))
"

echo "=== 3) 到 208 连通（Winbox 依赖 IP 可达）==="
ping -c2 -W2 $PEER 2>&1 | tail -3
ping -c2 -W2 -I eno1np0 $PEER 2>&1 | tail -3
ip vrf exec $VRF ping -c2 -W2 -I 139.159.43.249 $PEER 2>&1 | tail -3
nc -z -w2 $PEER 8291 2>&1; echo winbox_8291_rc=$?
ss -tn | grep $PEER | head -8

echo "=== 4) 上游库条数（通告来源规模）==="
curl -sf "http://127.0.0.1:9179/api/rib/routes/count?window=upstream&vrf=gobgp-rr&neighbor_ip=139.159.43.249"
echo

echo "=== 5) 最近通告/撤销日志 ==="
journalctl -u bgp-agent --since '6 hours ago' --no-pager 2>/dev/null | \
  grep -iE '208-advertise|208/|vbgp13915943249.*208|advertise|withdraw' | tail -25

echo "=== 6) 系统负载（灌路由时 CPU/内存）==="
uptime
free -h | head -2
top -bn1 | head -5

echo "=== 7) 208 静态邻居 / iv249 ==="
ip neigh show dev iv249 | grep 208 || true
ip route show vrf $VRF | grep 208 | head -5
"""

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    try:
        print(f"=== 109 ({host}) 下游 208 / 路由通告 诊断 ===\n")
        print(run(c, script))
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
