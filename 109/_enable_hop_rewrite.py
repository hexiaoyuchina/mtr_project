#!/usr/bin/env python3
"""开启逐跳替换并验证规则 #20。"""
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

echo "=== 开启总开关（会冷启动 te_rewrite + NFQUEUE）==="
curl -sf -X PUT http://127.0.0.1:8808/api/global \
  -H 'Content-Type: application/json' -d '{"hijack_enabled":true}'
echo
sleep 6

echo "=== 状态 ==="
curl -s http://127.0.0.1:8808/api/global
echo
cat /tmp/mtr_te_map.env
pgrep -af te_rewrite || echo NO_DAEMON
cat /proc/net/netfilter/nfnetlink_queue 2>/dev/null || echo EMPTY_QUEUE
iptables -t mangle -S FORWARD | grep NFQUEUE || echo NO_NFQUEUE
tail -5 /tmp/te_rewrite_nfqueue.log

echo "=== 20s 下联：应出现 100.100.100.100（请保持 mtr 8.8.8.8）==="
timeout 20 tcpdump -ni eno1np0 -c 12 'icmp[icmptype]==11 and (host 100.100.100.100 or host 142.251.67.15)' 2>&1 | head -16
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=60)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode(errors="replace")
    print(out)
    c.close()
    if "NO_NFQUEUE" in out or "EMPTY_QUEUE" in out.split("=== 状态")[-1][:500]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
