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
echo "=== uplink 15s: TE or echo-reply dst 105.94 ==="
timeout 15 tcpdump -ni enp59s0f0np0 -c 50 '(icmp[icmptype]==11 or icmp[icmptype]==0) and host 139.159.105.94' 2>&1 | head -50
echo "=== uplink 10s: any icmp11 ==="
timeout 10 tcpdump -ni enp59s0f0np0 -c 15 'icmp[icmptype]==11' 2>&1 | head -20
echo "=== NFQUEUE ==="
cat /proc/net/netfilter/nfnetlink_queue 2>/dev/null || true
echo "=== te_rewrite rewrite lines ==="
grep -E 'rewrite|142\.251|100\.100' /tmp/te_rewrite_nfqueue.log 2>/dev/null | tail -20 || echo '(none)'
echo "=== force te_rewrite reload ==="
kill -HUP $(pgrep -f te_rewrite_nfqueue.py | head -1) 2>/dev/null && echo HUP ok || echo HUP fail
sleep 1
tail -3 /tmp/te_rewrite_nfqueue.log
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=40)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
