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
pkill -9 -f te_rewrite_nfqueue.py 2>/dev/null || true
sleep 1
/usr/bin/python3 <<'PY' &
import os, time
from netfilterqueue import NetfilterQueue
def cb(pkt):
    pkt.accept()
nfq = NetfilterQueue()
nfq.bind(1, cb)
print("bind_ok", flush=True)
time.sleep(5)
nfq.unbind()
print("unbind_ok", flush=True)
PY
sleep 2
echo "=== queue after bind test ==="
cat /proc/net/netfilter/nfnetlink_queue | od -An -tx1 | head -2
echo "=== restart production ==="
export MTR_TE_REWRITE_MAP='142.251.67.15=100.100.100.100'
export MTR_TE_REWRITE_MAP_FILE=/tmp/mtr_te_map.env
nohup /usr/bin/python3 /root/mtr_op/te_rewrite_nfqueue.py >>/tmp/te_rewrite_nfqueue.log 2>&1 &
sleep 2
cat /proc/net/netfilter/nfnetlink_queue | od -An -tx1 | head -2
pgrep -af te_rewrite
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=20)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
