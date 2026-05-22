#!/usr/bin/env python3
"""Diagnose multi satellite VRF: 208 ping 245/247/249 on 109."""
from __future__ import annotations

import os
from pathlib import Path

import paramiko

DEPLOY_DIR = Path(__file__).resolve().parent


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


REMOTE_SCRIPT = r"""
set +e
SPOOFS="245 247 249"
PEER=139.159.43.208

echo '========== PROCESSES =========='
pgrep -af 'arp_spoof|bgp-agent' || true

echo
echo '========== SATELLITE VRF + IPVLAN =========='
for ip in $SPOOFS; do
  iv=iv${ip}
  vrf=vbgp13915943${ip}
  echo "--- spoof ${ip} vrf ${vrf} dev ${iv} ---"
  ip -br link show "$iv" 2>/dev/null || echo "  iv MISSING"
  ip addr show "$iv" 2>/dev/null | grep -E 'inet |master' || true
  echo "  routes vrf ${vrf}:"
  ip route show vrf "$vrf" 2>/dev/null | head -5
  echo "  rules from ${ip}:"
  ip -4 rule show 2>/dev/null | grep "139.159.43.${ip}" | head -3
  echo "  route get ${PEER} from ${ip}:"
  ip route get "$PEER" from "139.159.43.${ip}" 2>/dev/null | head -1
done

echo
echo '========== ARP LOG =========='
tail -20 /tmp/arp_spoof_daemon.log 2>/dev/null || echo no_log

echo
echo '========== 109 -> 208 ping per VRF =========='
for ip in $SPOOFS; do
  vrf=vbgp13915943${ip}
  iv=iv${ip}
  echo -n "from 139.159.43.${ip}: "
  ip vrf exec "$vrf" ping -c1 -W2 -I "$iv" "$PEER" 2>&1 | grep -E 'bytes from|100%|Unreachable|failed' || echo fail
done

echo
echo '========== NEIGH 208 on iv* =========='
for ip in $SPOOFS; do
  echo -n "iv${ip}: "
  ip neigh show "$PEER" dev "iv${ip}" 2>/dev/null || echo none
done

echo
echo '========== DB ARP rows 245/247/249 =========='
sqlite3 /root/mtr_op/data.db "SELECT spoof_gateway_ip, satellite_vrf, egress_iface, enabled, policy_mode FROM arp_spoof_targets WHERE spoof_gateway_ip IN ('139.159.43.245','139.159.43.247','139.159.43.249');"
echo '--- bgp_neighbor_meta ---'
sqlite3 /root/mtr_op/data.db "SELECT vrf, neighbor_ip, source_ip FROM bgp_neighbor_meta WHERE vrf LIKE 'vbgp13915943%';"

echo
echo '========== ipvlan state files =========='
ls -la /root/mtr_op/.bgp_ipvlan* 2>/dev/null || true

echo
echo '========== Agent neighbors (downstream) =========='
curl -sf http://127.0.0.1:9179/api/neighbors 2>/dev/null | head -c 8000 || echo agent_fail

echo
echo '========== icmp nft (spoof) =========='
nft list ruleset 2>/dev/null | grep -E '139.159.43.24[579]|echo-request' | head -12 || true

echo
echo '========== ipvlan reconcile json =========='
python3 -c "import json;d=json.load(open('/root/mtr_op/.bgp_ipvlan_reconcile.json'));b=d.get('by_spoof_ip') or {};\
[print(k,b.get(k)) for k in sorted(b) if k.endswith(('.245','.247','.249'))]"

echo
echo '========== simulate 208->spoof: local ping spoof IPs =========='
for ip in $SPOOFS; do
  echo -n "ping 139.159.43.${ip} from main: "
  ping -c1 -W1 139.159.43.${ip} 2>&1 | grep -E 'bytes from|100%' || echo fail
done
"""


def main() -> None:
    load_env()
    host = os.environ["MTR_OP_HOST"]
    user = os.environ.get("MTR_OP_SSH_USER", "root")
    password = os.environ["MTR_OP_SSH_PASSWORD"]
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=user, password=password, timeout=30, allow_agent=False, look_for_keys=False)
    _, stdout, stderr = c.exec_command(REMOTE_SCRIPT, timeout=120)
    print(stdout.read().decode("utf-8", errors="replace"))
    err = stderr.read().decode("utf-8", errors="replace")
    if err.strip():
        print("STDERR:", err)
    c.close()


if __name__ == "__main__":
    main()
