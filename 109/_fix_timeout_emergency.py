#!/usr/bin/env python3
"""109 MTR 全 timeout：诊断 + 拆掉 NFQUEUE 恢复 + 验证下联 TE。"""
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
set +e
echo "========== $(date) =========="
echo "--- BEFORE ---"
pgrep -af te_rewrite || echo no_te_rewrite
echo -n "queue bytes: "; wc -c </proc/net/netfilter/nfnetlink_queue 2>/dev/null
cat /proc/net/netfilter/nfnetlink_queue 2>/dev/null
iptables -t mangle -S FORWARD | grep -i nfqueue || echo no_nfqueue_rules
for pid in $(pgrep -f te_rewrite_nfqueue); do echo "pid $pid stat=$(awk '{print $3}' /proc/$pid/stat 2>/dev/null)"; done

echo "--- 8s uplink TE (mtr running?) ---"
timeout 8 tcpdump -ni enp59s0f0np0 -c 8 'icmp[icmptype]==11 and host 139.159.105.94' 2>&1 | head -12
echo "--- 8s downlink BEFORE fix ---"
timeout 8 tcpdump -ni eno1np0 -c 8 'icmp[icmptype]==11 and host 139.159.105.94' 2>&1 | head -12

echo "========== FIX: remove NFQUEUE, keep te_rewrite stopped =========="
pkill -9 -f te_rewrite_nfqueue 2>/dev/null || true
cd /root/mtr_op
./venv/bin/python3 -c "
import sys; sys.path.insert(0,'.')
from app import te_rewrite_sync
te_rewrite_sync.clear_iptables_nfqueue()
print('cleared')
"
iptables -t mangle -S FORWARD | grep -i nfqueue || echo "FORWARD: no NFQUEUE OK"

echo "--- 20s downlink AFTER fix (请保持 mtr) ---"
timeout 20 tcpdump -ni eno1np0 -c 15 'icmp[icmptype]==11 and host 139.159.105.94' 2>&1 | head -18
echo "--- 8s echo-reply downlink ---"
timeout 8 tcpdump -ni eno1np0 -c 6 'icmp[icmptype]==0 and host 139.159.105.94' 2>&1 | head -10
echo DONE
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=75)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
