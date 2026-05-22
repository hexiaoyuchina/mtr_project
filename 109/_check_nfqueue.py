#!/usr/bin/env python3
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
    s = r"""
echo "=== processes ==="
pgrep -af 'te_rewrite|uvicorn' || true
echo "=== nfnetlink_queue ==="
cat /proc/net/netfilter/nfnetlink_queue 2>/dev/null | head -3 || echo EMPTY
echo "=== mangle ==="
iptables -t mangle -S FORWARD 2>/dev/null | head -10
iptables -t mangle -S OUTPUT 2>/dev/null | head -8
echo "=== route get (fixed) ==="
ip route get 8.8.8.8 from 139.159.105.94 iif eno1np0 2>&1 | head -2
ip route get 139.159.105.94 from 8.8.8.8 iif enp59s0f0np0 2>&1 | head -2
echo "=== recent te/mtr log ==="
tail -3 /tmp/te_rewrite_nfqueue.log 2>/dev/null
tail -3 /tmp/te_rewrite_nfqueue.log 2>/dev/null
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=30)
    stdin.write(s)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
