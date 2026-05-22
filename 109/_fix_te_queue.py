#!/usr/bin/env python3
"""修复 te_rewrite 未 bind NFQUEUE 导致 MTR 全 timeout。"""
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
export MTR_BGP_IPVLAN_BASE_IFACE=eno1np0
export MTR_BGP_RR_UPLINK_IFACE=enp59s0f0np0

echo "=== before ==="
pgrep -af te_rewrite || true
cat /proc/net/netfilter/nfnetlink_queue 2>/dev/null || echo "(empty queue)"
iptables -t mangle -S FORWARD | grep -i NFQUEUE || true

echo "=== restart via te_rewrite_sync ==="
./venv/bin/python3 - <<'PY'
import os, sys
sys.path.insert(0, "/root/mtr_op")
from pathlib import Path
from app import storage
from app import te_rewrite_sync
db = Path(os.environ["MTR_OP_DB"])
conn = storage.connect(db)
te_rewrite_sync.sync_te_rewrite_from_conn(conn, flush_iptables_legacy=True)
conn.close()
print("sync_ok")
PY

sleep 3
echo "=== after ==="
pgrep -af te_rewrite || true
cat /proc/net/netfilter/nfnetlink_queue 2>/dev/null || echo "(still empty!)"
tail -5 /tmp/te_rewrite_nfqueue.log
echo "=== 8s downlink TE (forged src?) ==="
timeout 8 tcpdump -ni eno1np0 -c 12 'icmp[icmptype]==11 and host 139.159.105.94' 2>&1 | head -15
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=60)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode(errors="replace")
    print(out)
    c.close()
    if "still empty" in out and "(still empty!)" in out.split("after")[-1]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
