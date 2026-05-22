#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

DIR = Path(__file__).resolve().parent


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
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ["MTR_OP_HOST"].strip(),
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    script = r"""
echo "========== 2110 missing 208/32? =========="
ip route show table 2110 | grep 208 || echo "no 208 in 2110"

echo "========== ping 105.94 from 109 =========="
ping -c2 -W1 -I eno1np0 139.159.105.94 2>&1 || true

echo "========== OUTPUT mangle NFQUEUE =========="
iptables -t mangle -S OUTPUT 2>/dev/null | head -15 || true

echo "========== FORWARD mangle =========="
iptables -t mangle -S FORWARD 2>/dev/null | grep -E 'NFQUEUE|eno1np0|enp59' || true

echo "========== te_rewrite / mtr processes =========="
pgrep -af te_rewrite || true
tail -5 /tmp/te_rewrite_nfqueue.log 2>/dev/null || echo no_te_log

echo "========== OP hop rules enabled =========="
/root/mtr_op/venv/bin/python3 -c "
import sqlite3
c=sqlite3.connect('/root/mtr_op/data.db')
print('hijack', c.execute('select hijack_enabled from global_config').fetchone())
print('hop_rules', c.execute('select id,match_cidr,forged_src,enabled from hop_replace_rules').fetchall())
"

echo "========== static route 10 sync =========="
/root/mtr_op/venv/bin/python3 -c "
import json, pathlib
p=pathlib.Path('/root/mtr_op/.static_routes_applied.json')
print(p.read_text() if p.is_file() else 'no_applied_state')
"
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=60)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode())
    c.close()


if __name__ == "__main__":
    main()
