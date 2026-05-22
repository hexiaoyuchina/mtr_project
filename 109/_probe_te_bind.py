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
PID=$(pgrep -f 'te_rewrite_nfqueue.py' | head -1)
echo "pid=$PID"
ls -l /proc/$PID/fd 2>/dev/null | head -20
echo "=== test import ==="
/usr/bin/python3 -c "
try:
    from netfilterqueue import NetfilterQueue
    print('NetfilterQueue OK')
except Exception as e:
    print('NetfilterQueue FAIL', e)
try:
    from scapy.layers.inet import ICMP, IP
    print('scapy OK')
except Exception as e:
    print('scapy FAIL', e)
"
echo "=== 6s downlink TE ==="
timeout 6 tcpdump -ni eno1np0 -c 10 'icmp[icmptype]==11 and host 139.159.105.94' 2>&1 | head -15
echo "=== 6s downlink 100.100.100.100 ==="
timeout 6 tcpdump -ni eno1np0 -c 5 'icmp[icmptype]==11 and host 100.100.100.100' 2>&1 | head -8
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=25)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
