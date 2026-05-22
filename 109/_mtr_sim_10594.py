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
SRC=139.159.105.94
DST=8.8.8.8
DOWN=eno1np0
echo "WARN: 勿在 109 的 $DOWN 挂 $SRC/32，会破坏下联转发；本脚本仅做 route get + 抓包。"
echo "=== route get (forward) ==="
ip route get $DST from $SRC iif $DOWN 2>&1 | head -2
echo "=== route get (return) ==="
ip route get $SRC from $DST iif enp59s0f0np0 2>&1 | head -2
echo "=== tcpdump 12s (需 208 侧同时 mtr) ==="
timeout 12 tcpdump -ni $DOWN -c 20 "host $SRC and icmp" 2>&1 | head -20
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=90)
    stdin.write(s)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
