#!/usr/bin/env python3
"""109 上校验 105.94↔8.8.8.8 路径；208 无 SSH 时发起到 208 的探测并抓包。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

DIR = Path(__file__).resolve().parent
PEER = "139.159.43.208"
SRC = "139.159.105.94"
DST = "8.8.8.8"
DOWN = "eno1np0"
UP = "enp59s0f0np0"


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
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    script = rf"""
set -e
PEER={PEER!r}
SRC={SRC!r}
DST={DST!r}
DOWN={DOWN!r}
UP={UP!r}

echo "========== 208 SSH 22 =========="
nc -zv -w 3 $PEER 22 2>&1 || true
nc -zv -w 3 $PEER 8291 2>&1 || true

echo "========== 109 路径（与 208 上 mtr 源 {SRC} 一致）=========="
ip route get $DST from $SRC iif $DOWN 2>&1
ip route get $SRC from $DST iif $UP 2>&1

echo "========== 临时在 {DOWN} 挂 {SRC}/32 做本机发包测试（测完删除）=========="
ip addr add $SRC/32 dev $DOWN 2>/dev/null || true
echo "--- ping $DST from $SRC ---"
ping -c 3 -W 2 -I $SRC $DST 2>&1 | tail -5
echo "--- mtr from $SRC (若在 109 上模拟客户端) ---"
if command -v mtr >/dev/null 2>&1; then
  mtr -n -r -c 6 --local-address $SRC $DST 2>&1 | head -25
else
  traceroute -n -s $SRC -w 1 -q 1 -m 10 $DST 2>&1 | head -15
fi
ip addr del $SRC/32 dev $DOWN 2>/dev/null || true

echo "========== 对 208 发 ping（109 视角）=========="
ping -c 2 -W 2 $PEER

echo "========== 抓包 8s：下联入站 src=$SRC 或 去 8.8.8.8（需 208 侧同时 mtr）=========="
echo "(若 208 为 RouterOS 无 SSH，请在 208 上手工: /tool mtr address=$DST src-address=$SRC)"
timeout 8 tcpdump -ni $DOWN -c 20 'host $SRC or (host $DST and icmp)' 2>/dev/null | head -25 || echo "no tcpdump packets in window"

echo "========== conntrack / 最近 FORWARD 计数 =========="
iptables -t mangle -L FORWARD -n -v 2>/dev/null | grep -E 'NFQUEUE|eno1np0' | head -5 || true
sysctl net.ipv4.conf.all.rp_filter net.ipv4.conf.eno1np0.rp_filter 2>/dev/null || true
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=120)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
