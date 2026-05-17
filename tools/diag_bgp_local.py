#!/usr/bin/env python3
"""本端诊断：BGP 下游/RR 状态、路由、抓包、修复尝试。"""
import json
import os
import sys
import time

import paramiko

HOST = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
PW = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
OP_DIR = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").strip()


def run(c, cmd, timeout=60):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    return o.channel.recv_exit_status(), out, err


def main():
    if not PW:
        print("NO_PASSWORD")
        sys.exit(2)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username="root", password=PW, timeout=20)

    script = r"""#!/bin/bash
set -e
PEER=139.159.43.208
SPOOF=139.159.43.249
RR=139.159.43.249
SRC=139.159.43.207
VRF=vbgp13915943249
IF=enp59s0f0np0

echo "========== neighbors =========="
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -m json.tool 2>/dev/null || curl -sf http://127.0.0.1:9179/api/neighbors

echo "========== ping/trace =========="
ping -c 2 -W 1 $PEER 2>&1 || true
ip vrf exec $VRF ping -c 2 -W 1 -I $SPOOF $PEER 2>&1 || true

echo "========== routes =========="
ip vrf exec $VRF ip route get $PEER from $SPOOF
ip -4 rule show | grep -E "249|207" || true
ip addr show dev iv249 2>/dev/null | head -8

echo "========== ss bgp =========="
ss -tn state all '( dport = :179 or sport = :179 )' 2>/dev/null | head -20 || ss -tn | grep -E "179|183" | head -20

echo "========== tcpdump 12s (bgp to/from 208,249) =========="
timeout 12 tcpdump -ni any -c 40 "host $PEER and (port 179 or port 1830 or port 1831)" 2>&1 || true

echo "========== journal bgp-agent =========="
journalctl -u bgp-agent -n 25 --no-pager 2>/dev/null | tail -20

echo "========== nft/filter =========="
nft list ruleset 2>/dev/null | grep -E "179|drop|reject" | head -15 || iptables -L -n 2>/dev/null | head -10 || true
"""
    sftp = c.open_sftp()
    sftp.file("/tmp/mtr_bgp_diag.sh", "w").write(script)
    sftp.close()
    code, out, err = run(c, "bash /tmp/mtr_bgp_diag.sh", timeout=90)
    print(out)
    if err.strip():
        print("STDERR:", err[:800])
    c.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
