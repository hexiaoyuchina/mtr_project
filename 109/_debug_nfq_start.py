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
pkill -9 -f te_rewrite_nfqueue 2>/dev/null || true
sleep 1
modprobe nfnetlink_queue
echo "=== foreground 8s ==="
export MTR_TE_REWRITE_MAP='142.251.67.15=100.100.100.100'
timeout 8 /usr/bin/python3 /root/mtr_op/te_rewrite_nfqueue.py 2>&1 | head -5 &
FP=$!
sleep 3
echo "queue while fg:"
cat /proc/net/netfilter/nfnetlink_queue
ps -p $FP -o pid,stat,cmd 2>/dev/null || echo fg_done
wait $FP 2>/dev/null || true
echo "=== minimal bind 5s ==="
pkill -9 -f te_rewrite_nfqueue 2>/dev/null || true
/usr/bin/python3 -c "
from netfilterqueue import NetfilterQueue
import time
def cb(p): p.accept()
n=NetfilterQueue()
n.bind(1, cb)
print('minimal_bind_ok', flush=True)
time.sleep(4)
n.unbind()
" 2>&1
cat /proc/net/netfilter/nfnetlink_queue
echo "=== dmesg nf ==="
dmesg | tail -5 | grep -i nf || true
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=25)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
