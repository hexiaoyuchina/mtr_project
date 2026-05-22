#!/usr/bin/env python3
"""逐跳规则导致 MTR 超时：查 hop 规则、TE map、NFQUEUE、日志。"""
from __future__ import annotations

import os
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
        username="root",
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    script = """
echo "=== hop_replace_rules ==="
/root/mtr_op/venv/bin/python3 <<'PY'
import sqlite3
c = sqlite3.connect("/root/mtr_op/data.db")
print("hijack", c.execute("SELECT hijack_enabled FROM global_config").fetchone())
for r in c.execute(
    "SELECT id,enabled,match_cidr,forged_src,priority,delay_min_ms,delay_max_ms,icmp_ip_ttl,note "
    "FROM hop_replace_rules ORDER BY id"
):
    print(r)
PY
echo "=== mtr_te_map.env ==="
cat /tmp/mtr_te_map.env 2>/dev/null || true
echo "=== te log tail ==="
tail -15 /tmp/te_rewrite_nfqueue.log 2>/dev/null || true
echo "=== te_rewrite log tail ==="
tail -20 /tmp/te_rewrite_nfqueue.log 2>/dev/null || true
echo "=== nft mtr_te_snat ==="
nft list table ip mtr_te_snat 2>/dev/null | head -40
echo "=== route get ==="
ip route get 8.8.8.8 from 139.159.105.94 iif eno1np0 2>&1 | head -2
ip route get 139.159.105.94 from 8.8.8.8 iif enp59s0f0np0 2>&1 | head -2
echo "=== iptables mangle ==="
iptables -t mangle -S FORWARD
iptables -t mangle -S OUTPUT | head -8
echo "=== nft queue icmp ==="
nft list ruleset 2>/dev/null | grep -E 'queue|echo|icmp type' | head -30
echo "=== route get 100.100.100.100 ==="
ip route get 100.100.100.100 from 139.159.105.94 iif eno1np0 2>&1 | head -2
echo "=== blackhole 100.100.100 ==="
ip route show table main | grep 100.100.100 || echo none
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=30)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
