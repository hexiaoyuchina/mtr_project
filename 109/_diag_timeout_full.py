#!/usr/bin/env python3
"""MTR 全 timeout：NFQUEUE / 下联 TE / 转发 全链路诊断。"""
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
echo "=== 1. te_rewrite / queue ==="
pgrep -af te_rewrite || echo NO_TE_REWRITE
echo -n "nfnetlink_queue: "
wc -c </proc/net/netfilter/nfnetlink_queue 2>/dev/null || echo 0
cat /proc/net/netfilter/nfnetlink_queue 2>/dev/null
for pid in $(pgrep -f te_rewrite_nfqueue); do
  echo "pid $pid state=$(cat /proc/$pid/stat 2>/dev/null | awk '{print $3}')"
  ls -l /proc/$pid/fd 2>/dev/null | wc -l
done

echo "=== 2. iptables mangle (all NFQUEUE) ==="
iptables -t mangle -S FORWARD 2>/dev/null
iptables -t mangle -S OUTPUT 2>/dev/null | grep -i nfqueue

echo "=== 3. hijack / map ==="
cat /tmp/mtr_te_map.env 2>/dev/null
tail -5 /tmp/te_rewrite_nfqueue.log 2>/dev/null

echo "=== 4. routes ==="
ip -4 rule list | grep -E '^29:|^30:'
ip route show table 2110
ip route show table 2111
ip route get 8.8.8.8 from 139.159.105.94 iif eno1np0 2>&1 | head -1
ip route get 139.159.105.94 from 8.8.8.8 iif enp59s0f0np0 2>&1 | head -1

echo "=== 5. 20s 上联 TE (请此时在 208 上 mtr) ==="
timeout 20 tcpdump -ni enp59s0f0np0 -c 25 'icmp[icmptype]==11 and host 139.159.105.94' 2>&1 | head -30

echo "=== 6. 20s 下联 TE (应出现若 NFQUEUE 正常) ==="
timeout 20 tcpdump -ni eno1np0 -c 25 'icmp[icmptype]==11 and host 139.159.105.94' 2>&1 | head -30

echo "=== 7. 10s 下联 echo-reply ==="
timeout 10 tcpdump -ni eno1np0 -c 10 'icmp[icmptype]==0 and host 139.159.105.94' 2>&1 | head -15

echo "=== 8. conntrack / rp_filter ==="
sysctl net.ipv4.conf.all.rp_filter net.ipv4.conf.eno1np0.rp_filter net.ipv4.conf.enp59s0f0np0.rp_filter 2>/dev/null
nft list ruleset 2>/dev/null | grep -i drop | head -5
iptables -t filter -S FORWARD 2>/dev/null | head -8
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=90)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode(errors="replace")
    print(out)
    c.close()


if __name__ == "__main__":
    main()
