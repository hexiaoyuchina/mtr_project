#!/usr/bin/env python3
"""只读：Winbox 139.159.105.94:8699 从 109 各路径可达性。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

DEPLOY_DIR = Path(__file__).resolve().parent
MGMT_IP = "139.159.105.94"
MGMT_PORT = 8699
PEER = "139.159.43.208"


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


def main() -> int:
    load_env()
    pw = os.environ["MTR_OP_SSH_PASSWORD"]
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    script = f"""
set +e
MIP={MGMT_IP}
MP={MGMT_PORT}
PEER={PEER}
echo "========== 路由：如何到 $MIP =========="
ip route get $MIP 2>&1 | head -3
ip route get $MIP from 139.159.43.207 iif enp59s0f0np0 2>&1 | head -2
ip route get $MIP from 139.159.43.249 iif eno1np0 2>&1 | head -2
ip route | grep -E '105\\.94|43\\.208' | head -15

echo ""
echo "========== ping $MIP =========="
ping -c2 -W2 $MIP 2>&1 | tail -3
ping -c2 -W2 -I enp59s0f0np0 $MIP 2>&1 | tail -3
ping -c2 -W2 -I eno1np0 $MIP 2>&1 | tail -3

echo ""
echo "========== TCP $MIP:$MP (Winbox) =========="
timeout 3 bash -c "echo >/dev/tcp/$MIP/$MP" 2>/dev/null && echo tcp_ok || echo tcp_fail
nc -z -w3 $MIP $MP 2>&1; echo nc_rc=$?

echo ""
echo "========== 对比：业务 $PEER =========="
ping -c1 -W2 -I eno1np0 $PEER >/dev/null 2>&1 && echo peer_ping_ok || echo peer_ping_fail
timeout 2 bash -c "echo >/dev/tcp/$PEER/8291" 2>/dev/null && echo peer_8291_ok || echo peer_8291_fail

echo ""
echo "========== BGP 208 邻居（当前）=========="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors?vrf=vbgp13915943249" | python3 -c "
import json,sys
for n in json.load(sys.stdin):
    if n.get('neighbor_ip')=='139.159.43.208':
        print('enabled',n.get('enabled'),'state',n.get('session_state'),
              'adv',n.get('advertise_routes'),'sent',n.get('routes_sent'))
"

echo ""
echo "========== 109 上是否有 105.94 相关地址/路由 =========="
ip -br addr | grep -E '105\\.|43\\.' || true
"""
    i, o, e = c.exec_command("bash -se", timeout=90)
    i.write(script)
    i.channel.shutdown_write()
    print(f"从 109 ({host}) 探测 Winbox {MGMT_IP}:{MGMT_PORT}\n")
    print(o.read().decode(errors="replace"))
    if e.read().decode().strip():
        print("[stderr]", e.read().decode()[:300])
    c.close()
    return 0


if __name__ == "__main__":
    load_env()
    sys.exit(main())
