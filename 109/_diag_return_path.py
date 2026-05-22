#!/usr/bin/env python3
"""诊断 109 MTR 回程：2111 / rule 29 / 105.94 neigh / OP 静态路由库。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

DIR = Path(__file__).resolve().parent
REMOTE = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
        break


def main() -> None:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not pw:
        raise SystemExit("MTR_OP_SSH_PASSWORD required")
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    user = os.environ.get("MTR_OP_SSH_USER", "root").strip()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=user, password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    script = f"""
echo "========== OP static_routes (105.92) =========="
{REMOTE}/venv/bin/python3 -c "import sqlite3; c=sqlite3.connect('{REMOTE}/data.db');
[print(r) for r in c.execute(\\\"SELECT id,enabled,dst_cidr,gateway_ip,egress_iface,table_id FROM static_routes WHERE dst_cidr LIKE '%105.92%'\\\")]"

echo "========== ip rule 29/30 =========="
ip -4 rule list | grep -E '^29:|^30:' || echo "(no 29/30)"

echo "========== table 2110 =========="
ip route show table 2110

echo "========== table 2111 =========="
ip route show table 2111

echo "========== main (105.x / 208) =========="
ip route show table main | grep -E '105\\.9|43\\.208' || true

echo "========== forward 105.94 -> 8.8.8.8 =========="
ip route get 8.8.8.8 from 139.159.105.94 iif eno1np0 2>&1 | head -3

echo "========== return 105.94 from uplink =========="
ip route get 139.159.105.94 from 8.8.8.8 iif enp59s0f0np0 2>&1 | head -3

echo "========== local to 105.94 =========="
ip route get 139.159.105.94 2>&1 | head -3

echo "========== neigh eno1np0 (208/105.94) =========="
ip neigh show dev eno1np0 | grep -E '43\\.208|105\\.94' || true

echo "========== sysctl forward =========="
sysctl -n net.ipv4.ip_forward

echo "========== transit persist script =========="
ls -la /usr/local/sbin/mtr-op-downstream-transit.sh 2>/dev/null || echo missing
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=45)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode("utf-8", errors="replace")
    print(out, end="")
    c.close()


if __name__ == "__main__":
    main()
