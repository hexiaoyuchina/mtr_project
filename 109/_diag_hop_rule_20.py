#!/usr/bin/env python3
"""诊断 hop 规则 #20：142.251.67.15 -> 100.100.100.100 是否生效。"""
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
echo "=== 时间 ==="
date
echo "=== hop rule 20 ==="
/root/mtr_op/venv/bin/python3 <<'PY'
import sqlite3
c = sqlite3.connect("/root/mtr_op/data.db")
print("hijack", c.execute("SELECT hijack_enabled FROM global_config").fetchone())
for r in c.execute(
    "SELECT id,enabled,match_cidr,forged_src,priority,note FROM hop_replace_rules WHERE id=20 OR match_cidr LIKE '%142.251%'"
):
    print(r)
PY
echo "=== TE map ==="
cat /tmp/mtr_te_map.env 2>/dev/null || echo missing
echo "=== 进程 / 队列 ==="
pgrep -af te_rewrite || true
echo "=== te_rewrite log (last 25) ==="
tail -25 /tmp/te_rewrite_nfqueue.log 2>/dev/null || true
echo "=== te_rewrite log (last 15) ==="
tail -15 /tmp/te_rewrite_nfqueue.log 2>/dev/null || true
echo "=== nft TE SNAT counters ==="
nft list chain ip mtr_te_snat postrouting 2>/dev/null
echo "=== 静态路由 / 路径 ==="
ip route show table 2110 | head -5
ip route show table 2111 | head -5
ip route get 8.8.8.8 from 139.159.105.94 iif eno1np0 2>&1 | head -2
ip route get 139.159.105.94 from 8.8.8.8 iif enp59s0f0np0 2>&1 | head -2
echo "=== 15s 抓包：TE 源 142.251 或 forged 100.100.100.100（需 208 同时 mtr）==="
timeout 15 tcpdump -ni any -c 30 '(icmp[icmptype]==11) and (host 142.251.67.15 or host 100.100.100.100)' 2>&1 | head -35 || true
echo "=== 15s 抓包：下联 105.94 任意 icmp ==="
timeout 15 tcpdump -ni eno1np0 -c 20 'host 139.159.105.94 and icmp' 2>&1 | head -25 || true
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=45)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
