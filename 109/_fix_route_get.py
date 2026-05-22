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
echo "=== eno1np0 addrs ==="
ip -4 addr show dev eno1np0
echo "=== del stray 105.94/32 ==="
ip addr del 139.159.105.94/32 dev eno1np0 2>/dev/null || true
ip route flush table 2110
ip route flush table 2111
ip route replace table 2110 139.159.43.208/32 dev eno1np0 scope link
ip route replace table 2110 139.159.43.0/24 dev eno1np0 scope link
ip route replace table 2110 default via 139.159.43.249 dev enp59s0f0np0 src 139.159.43.207
ip route replace table 2111 139.159.105.92/30 dev eno1np0 scope link
ip route replace table 2111 139.159.43.208/32 dev eno1np0 scope link
ip -4 rule del pref 30 2>/dev/null || true
ip -4 rule add pref 30 iif eno1np0 lookup 2110
while ip -4 rule del pref 29 to 139.159.105.92/30 2>/dev/null; do :; done
ip -4 rule add pref 29 to 139.159.105.92/30 lookup 2111
echo "=== route get after fix ==="
ip route get 8.8.8.8 from 139.159.105.94 iif eno1np0 2>&1 | head -2
ip route get 139.159.105.94 from 8.8.8.8 iif enp59s0f0np0 2>&1 | head -2
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=30)
    stdin.write(s)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
