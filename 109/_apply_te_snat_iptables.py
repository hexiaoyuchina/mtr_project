#!/usr/bin/env python3
"""109：不用 NFQUEUE，用 iptables nat SNAT 改写 TE 外层源（实验室转发面可命中）。"""
from __future__ import annotations

import os
from pathlib import Path

import paramiko

DIR = Path(__file__).resolve().parent
MAP = ("142.251.67.15", "100.100.100.100")
DOWN = "eno1np0"
UP = "enp59s0f0np0"


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
    old, new = MAP
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
    script = f"""
set -e
pkill -9 -f te_rewrite_nfqueue 2>/dev/null || true
cd /root/mtr_op
./venv/bin/python3 -c "
import sys; sys.path.insert(0,'/root/mtr_op')
from app import te_rewrite_sync
te_rewrite_sync.clear_iptables_nfqueue()
print('nfqueue_cleared')
"
# 清理旧 SNAT
while iptables -t nat -D POSTROUTING -p icmp -m icmp --icmp-type 11 -s {old} -o {DOWN} -j SNAT --to-source {new} 2>/dev/null; do :; done
while iptables -t nat -D POSTROUTING -p icmp -m icmp --icmp-type 11 -s {old} -o {DOWN} -j SNAT --to-source {new} 2>/dev/null; do :; done
iptables -t nat -A POSTROUTING -p icmp -m icmp --icmp-type 11 -s {old} -o {DOWN} -j SNAT --to-source {new}
iptables -t nat -L POSTROUTING -n -v | grep -E 'time-exceeded|{new}' || true
echo "=== 12s downlink: expect {new} instead of {old} ==="
timeout 12 tcpdump -ni {DOWN} -c 8 'icmp[icmptype]==11 and (host {new} or host {old})' 2>&1 | head -12
echo done
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=30)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
