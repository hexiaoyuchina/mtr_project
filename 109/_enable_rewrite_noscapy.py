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
    root = Path(__file__).resolve().parent.parent
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
    sftp = c.open_sftp()
    sftp.put(str(root / "scripts" / "te_rewrite_nfqueue.py"), "/root/mtr_op/te_rewrite_nfqueue.py")
    sftp.put(
        str(root / "service" / "app" / "te_rewrite_sync.py"),
        "/root/mtr_op/app/te_rewrite_sync.py",
    )
    sftp.close()
    script = r"""
set -e
cd /root/mtr_op
export MTR_OP_DB=/root/mtr_op/data.db
export MTR_TE_REWRITE_SCRIPT=/root/mtr_op/te_rewrite_nfqueue.py
export MTR_TE_REWRITE_OIF=eno1np0
export MTR_TE_REWRITE_IIF=enp59s0f0np0
pkill -9 -f te_rewrite_nfqueue 2>/dev/null || true
sleep 1
./venv/bin/python3 - <<'PY'
import sys
sys.path.insert(0, "/root/mtr_op")
from pathlib import Path
from app import storage, te_rewrite_sync
conn = storage.connect(Path("/root/mtr_op/data.db"))
storage.set_global(conn, True)
conn.commit()
te_rewrite_sync.sync_te_rewrite_from_conn(conn, flush_iptables_legacy=True)
conn.close()
print("ok")
PY
sleep 2
cat /tmp/mtr_te_map.env
pgrep -af te_rewrite
cat /proc/net/netfilter/nfnetlink_queue
iptables -t mangle -S FORWARD | grep NFQUEUE
tail -4 /tmp/te_rewrite_nfqueue.log
echo "=== 15s downlink ==="
timeout 15 tcpdump -ni eno1np0 -c 8 'host 100.100.100.100 and icmp[icmptype]==11' 2>&1 | head -10
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=60)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
