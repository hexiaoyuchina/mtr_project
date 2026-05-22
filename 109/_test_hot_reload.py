#!/usr/bin/env python3
"""验证改 hop 规则不会 pkill te_rewrite。"""
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
PID1=$(pgrep -f 'te_rewrite_nfqueue.py' | head -1)
echo "before pid=$PID1"
curl -sf -X PATCH http://127.0.0.1:8808/api/hop-rules/20 \
  -H 'Content-Type: application/json' -d '{"enabled":true,"note":"hot-test"}' | head -c 120
echo
sleep 1
PID2=$(pgrep -f 'te_rewrite_nfqueue.py' | head -1)
echo "after pid=$PID2"
cat /proc/net/netfilter/nfnetlink_queue
iptables -t mangle -S FORWARD | grep -c NFQUEUE
tail -3 /tmp/mtr_op.log 2>/dev/null | grep -i te_rewrite || tail -2 /tmp/te_rewrite_nfqueue.log
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=20)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
