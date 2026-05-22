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
set -e
cd /root/mtr_op
export MTR_OP_DB=/root/mtr_op/data.db
export MTR_TE_REWRITE_SCRIPT=/root/mtr_op/te_rewrite_nfqueue.py
export MTR_TE_REWRITE_OIF=eno1np0
export MTR_TE_REWRITE_IIF=enp59s0f0np0
export MTR_OP_NFT=/root/mtr_op/nft_mtr_te.nft
pkill -9 -f te_rewrite_nfqueue 2>/dev/null || true
# 先预热 scapy（避免守护进程 bind 前卡太久）
echo "warming scapy..."
/usr/bin/python3 -c "from scapy.layers.inet import ICMP, IP; print('scapy_ok')"
# 后台起 uvicorn（若未运行）
pgrep -f 'uvicorn app.main' >/dev/null || {
  nohup ./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8808 >>/tmp/mtr_op.log 2>&1 &
  sleep 8
}
curl -sf http://127.0.0.1:8808/health && echo health_ok

./venv/bin/python3 - <<'PY'
import sys
sys.path.insert(0, "/root/mtr_op")
from pathlib import Path
from app import storage, te_rewrite_sync

conn = storage.connect(Path("/root/mtr_op/data.db"))
storage.set_global(conn, True)
conn.commit()
line = te_rewrite_sync.build_rewrite_map_line(conn)
te_rewrite_sync.sync_te_rewrite_from_conn(conn, flush_iptables_legacy=True)
conn.close()
print("sync_done")
PY

sleep 3
cat /tmp/mtr_te_map.env
pgrep -af te_rewrite
cat /proc/net/netfilter/nfnetlink_queue
iptables -t mangle -S FORWARD | grep NFQUEUE
tail -8 /tmp/te_rewrite_nfqueue.log
echo "=== 20s downlink ==="
timeout 20 tcpdump -ni eno1np0 -c 10 'icmp[icmptype]==11 and host 100.100.100.100' 2>&1 | head -12
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=180)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
