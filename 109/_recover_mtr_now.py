#!/usr/bin/env python3
"""109：恢复 MTR（不 pkill），确保 queue bind + iptables。"""
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
modprobe nfnetlink_queue 2>/dev/null || true

./venv/bin/python3 - <<'PY'
import sys
sys.path.insert(0, "/root/mtr_op")
from pathlib import Path
from app import storage, te_rewrite_sync

conn = storage.connect(Path("/root/mtr_op/data.db"))
# 冷启动一次（仅当 queue 未绑定）
if not te_rewrite_sync._nfqueue_has_listener():
    te_rewrite_sync.sync_te_rewrite_from_conn(conn, flush_iptables_legacy=True)
else:
    line = te_rewrite_sync.build_rewrite_map_line(conn)
    te_rewrite_sync.write_te_map_env(line)
    te_rewrite_sync.reload_te_rewrite_daemon()
    te_rewrite_sync.ensure_iptables_nfqueue(flush_legacy=False)
conn.close()
print("recover_ok")
PY

echo "queue:"; cat /proc/net/netfilter/nfnetlink_queue
iptables -t mangle -S FORWARD | grep NFQUEUE || echo NO_NFQUEUE
echo "=== 18s downlink (请正在 mtr) ==="
timeout 18 tcpdump -ni eno1np0 -c 10 'icmp[icmptype]==11 and host 139.159.105.94' 2>&1 | head -14
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=45)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode(errors="replace")
    print(out)
    c.close()


if __name__ == "__main__":
    main()
