#!/usr/bin/env python3
"""Read-only: diagnose vbgp13915943249 -> 208 BGP (spoof 249)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import paramiko

DEPLOY_DIR = Path(__file__).resolve().parent

REMOTE = r"""
set +e
VRF=vbgp13915943249
IV=iv249
SPOOF=139.159.43.249
PEER=139.159.43.208
PORT=1830

echo '========== BGP TCP :179 / TX port =========='
ss -tnp 2>/dev/null | grep -E ':179|:1830|208|249' || true

echo
echo '========== Agent health + status =========='
curl -sf http://127.0.0.1:9179/health && echo
curl -s http://127.0.0.1:9179/api/status 2>/dev/null | python3 -m json.tool 2>/dev/null | head -80

echo
echo '========== Agent neighbors (grep 208/249/vbgp) =========='
curl -s 'http://127.0.0.1:9179/api/neighbors?vrf='"$VRF" 2>/dev/null | python3 -m json.tool 2>/dev/null || \
  curl -s http://127.0.0.1:9179/api/status 2>/dev/null | grep -E '208|249|vbgp|Active|Established|pfx' | head -30

echo
echo '========== OP gobgp status (8808) =========='
curl -s http://127.0.0.1:8808/api/gobgp/status 2>/dev/null | python3 -m json.tool 2>/dev/null | head -100

echo
echo '========== Kernel VRF / iv249 =========='
ip -br link show "$VRF" "$IV" eno1np0 2>/dev/null
ip addr show "$IV" 2>/dev/null | grep -E 'inet |master'
echo 'routes vrf:' 
ip route show vrf "$VRF" 2>/dev/null
echo 'rule from 249:'
ip -4 rule list | grep 249 | head -8
echo 'route get 208 from 249:'
ip route get "$PEER" from "$SPOOF" 2>&1 | head -2
echo 'ping 208 from vrf:'
ip vrf exec "$VRF" ping -c2 -W2 -I "$IV" "$PEER" 2>&1 | tail -3

echo
echo '========== nft DNAT 249:179 =========='
nft list table inet mtr_bgp_sat_dnat 2>/dev/null | grep -E '249|1830|eno1np0' || echo 'no dnat or no match'

echo
echo '========== DB meta =========='
sqlite3 /root/mtr_op/data.db \
  "SELECT vrf,neighbor_ip,source_ip,role,advertise_routes,store_received_routes FROM bgp_neighbor_meta WHERE vrf='$VRF' OR neighbor_ip='$PEER';"
sqlite3 /root/mtr_op/data.db \
  "SELECT spoof_gateway_ip,satellite_vrf,egress_iface,enabled FROM arp_spoof_targets WHERE spoof_gateway_ip='$SPOOF';"

echo
echo '========== listen + syn to 208 =========='
ss -ltnp | grep -E ':179|:183' || true
ss -tnp state syn-sent,syn-recv,time-wait 2>/dev/null | grep -E '208|1830|249' || echo 'no syn to 208'

echo
echo '========== neighbor 208 full json =========='
curl -s 'http://127.0.0.1:9179/api/neighbors?vrf=vbgp13915943249' 2>/dev/null

echo
echo '========== meta 208 =========='
sqlite3 /root/mtr_op/data.db "SELECT * FROM bgp_neighbor_meta WHERE neighbor_ip='139.159.43.208';"

echo '========== Agent log (208/249/passive/Active) =========='
journalctl -u bgp-agent -n 40 --no-pager 2>/dev/null | grep -iE '208|249|passive|active|estab|error|1830' | tail -20
"""


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY_DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()
        if name == "env":
            break


def main() -> None:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        print("need MTR_OP_SSH_PASSWORD", file=sys.stderr)
        sys.exit(2)
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=60)
    stdin.write(REMOTE)
    stdin.channel.shutdown_write()
    stdout.channel.settimeout(65)
    print(stdout.read().decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
