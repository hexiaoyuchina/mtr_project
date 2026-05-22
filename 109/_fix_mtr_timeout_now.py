#!/usr/bin/env python3
"""109：先 bind NFQUEUE 再装 iptables，恢复 MTR 回程 TE。"""
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
modprobe nfnetlink_queue 2>/dev/null || true

echo "=== 1. 拆掉 NFQUEUE（先让 TE 可直通）==="
./venv/bin/python3 - <<'PY'
import sys
sys.path.insert(0, "/root/mtr_op")
from app import te_rewrite_sync
te_rewrite_sync.clear_iptables_nfqueue()
print("iptables_nfqueue_cleared")
PY
pkill -9 -f te_rewrite_nfqueue.py 2>/dev/null || true
sleep 1

echo "=== 2. 先起守护进程并等待 bind ==="
export MTR_TE_REWRITE_MAP='142.251.67.15=100.100.100.100'
export MTR_TE_REWRITE_MAP_FILE=/tmp/mtr_te_map.env
export MTR_TE_QUEUE_NUM=1
: >> /tmp/te_rewrite_nfqueue.log
nohup /usr/bin/python3 -u /root/mtr_op/te_rewrite_nfqueue.py >>/tmp/te_rewrite_nfqueue.log 2>&1 &
for i in $(seq 1 120); do
  if [ -s /proc/net/netfilter/nfnetlink_queue ]; then
    echo "bind_ok after ${i}x0.25s"
    cat /proc/net/netfilter/nfnetlink_queue
    break
  fi
  sleep 0.25
done
if [ ! -s /proc/net/netfilter/nfnetlink_queue ]; then
  echo "FATAL: NFQUEUE not bound"
  tail -20 /tmp/te_rewrite_nfqueue.log
  exit 1
fi

echo "=== 3. 再装 iptables NFQUEUE ==="
./venv/bin/python3 - <<'PY'
import sys
sys.path.insert(0, "/root/mtr_op")
from app import te_rewrite_sync
te_rewrite_sync.ensure_iptables_nfqueue(flush_legacy=True)
print("iptables_nfqueue_installed")
PY

echo "=== 4. 15s 下联 TE（请保持 mtr）==="
timeout 15 tcpdump -ni eno1np0 -c 12 'icmp[icmptype]==11 and host 139.159.105.94' 2>&1 | head -18
echo DONE
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=60)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode(errors="replace")
    print(out)
    c.close()
    if "FATAL" in out or "0 packets captured" in out.split("=== 4.")[-1][:200]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
