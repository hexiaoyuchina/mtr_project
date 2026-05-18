#!/usr/bin/env python3
"""对比 233 / 235 卫星 BGP 在 Linux 200 上的差异。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    raise SystemExit(2)

LAB = Path(__file__).resolve().parent
IPS = ("10.133.152.233", "10.133.152.235")
VRFS = ("vbgp10133152233", "vbgp10133152235")
PEER = "10.133.152.204"


def load_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def bash(c: paramiko.SSHClient, script: str) -> str:
    i, o, e = c.exec_command("bash -se", timeout=120)
    i.write(script)
    i.channel.shutdown_write()
    return o.read().decode(errors="replace") + e.read().decode(errors="replace")


def main() -> None:
    load_env()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ["MTR_OP_HOST"],
        username="root",
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=25,
        allow_agent=False,
        look_for_keys=False,
    )

    script = f"""
set -e
echo '======== OP ARP targets ========'
curl -sf http://127.0.0.1:8808/api/arp-spoof/targets | python3 -m json.tool

echo '======== OP BGP neighbors ========'
curl -sf http://127.0.0.1:8808/api/bgp/neighbors | python3 -m json.tool

echo '======== Agent neighbors ========'
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -m json.tool

echo '======== freeze ========'
curl -sf http://127.0.0.1:9179/api/peers/freeze-status | python3 -m json.tool

for ip in 233 235; do
  echo "======== kernel iv$ip / vrf ========"
  ip -br addr show iv$ip 2>/dev/null || echo "no iv$ip"
  vrf=vbgp101331522$ip
  ip link show $vrf 2>/dev/null | head -1 || echo "no $vrf"
  ip route show vrf $vrf 2>/dev/null || echo "no routes vrf $vrf"
  ip rule show | grep 152.$ip || echo "no policy rule for 152.$ip"
done

echo '======== nft dnat ========'
nft list table inet mtr_bgp_sat_dnat 2>/dev/null || echo no-table

echo '======== BGP TCP ========'
ss -tnp | grep -E '152\\.(233|235)|152\\.204:179' || true

echo '======== agent logs 235/233 (last 40) ========'
journalctl -u bgp-agent -n 80 --no-pager 2>/dev/null | grep -E '235|233|vbgp101331522' | tail -40 || true
"""
    print(bash(c, script))
    c.close()


if __name__ == "__main__":
    main()
