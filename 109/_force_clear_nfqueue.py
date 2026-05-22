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
    script = r"""
set -x
pkill -9 -f te_rewrite_nfqueue 2>/dev/null || true
sleep 1
# 强制删光 mangle 里所有 NFQUEUE
while iptables -t mangle -D FORWARD -p icmp -m icmp --icmp-type time-exceeded -j NFQUEUE --queue-num 1 2>/dev/null; do echo del_fwd; done
while iptables -t mangle -D FORWARD -o eno1np0 -p icmp -m icmp --icmp-type 11 -j NFQUEUE --queue-num 1 2>/dev/null; do echo del_fwd_o; done
while iptables -t mangle -D FORWARD -i enp59s0f0np0 -o eno1np0 -p icmp -m icmp --icmp-type 11 -j NFQUEUE --queue-num 1 2>/dev/null; do echo del_fwd_io; done
while iptables -t mangle -D OUTPUT -p icmp -m icmp --icmp-type time-exceeded -j NFQUEUE --queue-num 1 2>/dev/null; do echo del_out; done
while iptables -t mangle -D OUTPUT -o eno1np0 -p icmp -m icmp --icmp-type 11 -j NFQUEUE --queue-num 1 2>/dev/null; do echo del_out_o; done
while iptables -t mangle -D OUTPUT -o enp59s0f0np0 -p icmp -m icmp --icmp-type 11 -j NFQUEUE --queue-num 1 2>/dev/null; do echo del_out_u; done
while iptables -t mangle -D OUTPUT -o ens192 -p icmp -m icmp --icmp-type 11 -j NFQUEUE --queue-num 1 2>/dev/null; do echo del_out_e192; done
while iptables -t mangle -D OUTPUT -o ens224 -p icmp -m icmp --icmp-type 11 -j NFQUEUE --queue-num 1 2>/dev/null; do echo del_out_e224; done
echo "=== FORWARD after ==="
iptables -t mangle -S FORWARD
echo "=== OUTPUT nfqueue ==="
iptables -t mangle -S OUTPUT | grep -i nfqueue || echo none
echo "=== 25s downlink TE ==="
timeout 25 tcpdump -ni eno1np0 -c 20 'icmp[icmptype]==11 and host 139.159.105.94' 2>&1 | head -22
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=50)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
