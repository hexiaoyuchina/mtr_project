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
cd /root/mtr_op
export MTR_OP_DB=/root/mtr_op/data.db
export MTR_TE_REWRITE_SCRIPT=/root/mtr_op/te_rewrite_nfqueue.py
export MTR_TE_REWRITE_OIF=eno1np0
export MTR_TE_REWRITE_IIF=enp59s0f0np0
./venv/bin/python3 - <<'PY'
import sys, time
sys.path.insert(0, "/root/mtr_op")
from pathlib import Path
from app import storage, te_rewrite_sync

conn = storage.connect(Path("/root/mtr_op/data.db"))
g = storage.get_global(conn)
print("hijack", g.hijack_enabled)
line = te_rewrite_sync.build_rewrite_map_line(conn)
print("map", line)
te_rewrite_sync.write_te_map_env(line)
for i in range(180):
    if te_rewrite_sync._nfqueue_has_listener():
        print("queue bound at", i * 0.5, "s")
        break
    time.sleep(0.5)
else:
    print("queue NOT bound, cold start")
    te_rewrite_sync.sync_te_rewrite_from_conn(conn, flush_iptables_legacy=True)
    time.sleep(2)
if te_rewrite_sync._nfqueue_has_listener():
    te_rewrite_sync.ensure_iptables_nfqueue(flush_legacy=True)
    print("iptables NFQUEUE installed")
else:
    print("FATAL no queue listener")
conn.close()
PY
iptables -t mangle -S FORWARD | grep NFQUEUE
cat /proc/net/netfilter/nfnetlink_queue
for pid in $(pgrep -f te_rewrite_nfqueue); do echo pid=$pid stat=$(awk '{print $3}' /proc/$pid/stat); done
echo "=== 25s downlink (mtr 时请看到 100.100.100.100) ==="
timeout 25 tcpdump -ni eno1np0 -c 15 'icmp[icmptype]==11 and (host 100.100.100.100 or host 142.251.67.15)' 2>&1 | head -18
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=90)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode(errors="replace")
    print(out)
    c.close()


if __name__ == "__main__":
    main()
