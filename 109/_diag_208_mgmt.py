#!/usr/bin/env python3
"""109 只读排查：208 BGP 断 + 管理口连不上（不改配置）。"""
from __future__ import annotations

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
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()
        if name == "env":
            break


def run(c: paramiko.SSHClient, script: str, timeout: int = 120) -> str:
    i, o, e = c.exec_command("bash -se", timeout=timeout)
    i.write(script)
    i.channel.shutdown_write()
    return o.read().decode(errors="replace") + e.read().decode(errors="replace")


def main() -> None:
    load_env()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109")
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        print("需要 109/env 中的 MTR_OP_SSH_PASSWORD", file=sys.stderr)
        raise SystemExit(2)

    script = r"""
set +e
echo "=== A) 管理口 / 默认路由 / 策略路由 ==="
ip -br addr show enp59s0f1np1 enp59s0f0np0 eno1np0 2>/dev/null
ip route show default
ip rule list | head -25
ip route get 101.89.68.109 from 139.159.43.207 iif enp59s0f0np0 2>&1 | head -3
ip route get 101.89.68.109 2>&1 | head -3

echo ""
echo "=== B) 249/208 主机路由与 VRF ==="
ip route show 139.159.43.249/32 2>/dev/null
ip route show 139.159.43.208/32 2>/dev/null
ip link show iv249 2>/dev/null | head -3
ip addr show iv249 2>/dev/null | head -5
ip -d link show vrf vbgp13915943249 2>/dev/null | head -5
ip route show table all 2>/dev/null | grep -E '139.159.43.(208|249)|vbgp13915943249' | head -20

echo ""
echo "=== C) nft 与 179 重定向 ==="
nft list table inet mtr_bgp_sat_dnat 2>/dev/null | head -25
nft list table inet mtr_bgp_spoof_rr 2>/dev/null | head -15
ss -lntp | grep -E ':179|:18[0-3][0-9]' | head -15
ss -tn state established '( dport = :179 or sport = :179 )' 2>/dev/null | grep -E '208|249|207' | head -10

echo ""
echo "=== D) Agent 邻居 208 / 249 / vbgp43249 ==="
curl -sf http://127.0.0.1:9179/api/neighbors 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
for n in d.get('neighbors') or []:
    a=str(n.get('address') or '')
    v=str(n.get('vrf') or '')
    if a in ('139.159.43.208','139.159.43.249') or '43249' in v:
        print(json.dumps(n,ensure_ascii=False))
" 2>/dev/null

echo ""
echo "=== E) 到 208 / 249 ping（多源）==="
ping -c1 -W2 -I enp59s0f0np0 139.159.43.249 2>&1 | tail -2
ping -c1 -W2 -I enp59s0f0np0 139.159.43.208 2>&1 | tail -2
ping -c1 -W2 -I eno1np0 139.159.43.208 2>&1 | tail -2
ip vrf exec vbgp13915943249 ping -c1 -W2 -I 139.159.43.249 139.159.43.208 2>&1 | tail -3

echo ""
echo "=== F) ARP 引流 DB 摘要 ==="
python3 <<'PY' 2>/dev/null
import sqlite3
for db in ('/root/mtr_op/data.db','/root/mtr_op/data/mtr.db'):
    try:
        c=sqlite3.connect(db)
        print('DB', db)
        for r in c.execute("SELECT spoof_gateway_ip, egress_interface, enabled FROM arp_spoof_targets LIMIT 10"):
            print(' arp_target', r)
        for r in c.execute("SELECT vrf, neighbor_ip, source_ip, enabled, advertise_routes FROM bgp_neighbor_meta WHERE neighbor_ip IN ('139.159.43.208','139.159.43.249') OR vrf LIKE '%43249%'"):
            print(' meta', r)
        c.close()
    except Exception as e:
        print(db, e)
PY

echo ""
echo "=== G) 最近 bgp-agent / 内核日志 ==="
journalctl -u bgp-agent --since '2 hours ago' --no-pager 2>/dev/null | grep -iE '208|249|open|fail|error|eno1|iv249|mgmt|68.109' | tail -25
dmesg 2>/dev/null | tail -15
"""

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    except Exception as e:
        print(f"SSH {host} 失败: {e}")
        print("\n（无法登录现场时，以下根据文档与架构做原因分析）")
        raise SystemExit(1)
    try:
        print(f"=== 109 主机 {host} 只读诊断 ===\n")
        print(run(c, script))
    finally:
        c.close()


if __name__ == "__main__":
    main()
