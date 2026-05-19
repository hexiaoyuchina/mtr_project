#!/usr/bin/env python3
"""排查 200↔201 下游 vbgp10133153204 / 152.204 / 冒充 153.204 为何 Active。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H200, H201 = "10.133.151.200", "10.133.151.201"
VRF = "vbgp10133153204"
PEER = "10.133.152.204"
SPOOF = "10.133.153.204"
RR = "10.133.153.204"
LOCAL_RR = "10.133.153.200"


def load_env() -> str:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def run(host: str, pw: str, script: str, timeout: int = 120) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=45, allow_agent=False, look_for_keys=False, banner_timeout=45)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    pw = load_env()
    print("=" * 60)
    print(f"Linux 200 @ {H200}")
    print("=" * 60)
    print(
        run(
            H200,
            pw,
            f"""
set -x
VRF={VRF}
PEER={PEER}
SPOOF={SPOOF}
echo '--- services ---'
systemctl is-active bgp-agent mtr-op 2>/dev/null; pgrep -af 'uvicorn|bgp_agent' | head -5
echo '--- ipvlan / spoof ---'
ip -br link show master "$VRF" 2>/dev/null || ip -br link | grep -E 'iv|vbgp' || true
ip -br addr show master "$VRF" 2>/dev/null || true
ip -br addr | grep -E '{SPOOF}|ens192' || true
echo '--- vrf route to peer ---'
ip route show vrf "$VRF" 2>/dev/null | head -15
ip route get {PEER} vrf "$VRF" 2>/dev/null || true
echo '--- ping peer from vrf ---'
ip vrf exec "$VRF" ping -c2 -W2 {PEER} 2>&1 || true
ip vrf exec "$VRF" ping -c2 -W2 -I {SPOOF} {PEER} 2>&1 || true
echo '--- nft dnat (sat) ---'
nft list table inet mtr_bgp_sat_dnat 2>/dev/null | head -25 || echo 'no sat dnat table'
echo '--- tcp :179 ---'
ss -tnp | grep -E '{PEER}|:179' | head -20
echo '--- agent neighbors ---'
curl -sf --max-time 15 http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('vrf')=='{VRF}' or n.get('address')=='{PEER}' or n.get('address')=='{RR}':
    print(json.dumps(n,indent=2))
" 2>/dev/null || echo agent_neighbors_fail
echo '--- freeze-status ---'
curl -sf --max-time 10 http://127.0.0.1:9179/api/peers/freeze-status | python3 -m json.tool 2>/dev/null | head -40
echo '--- rr status ---'
curl -sf --max-time 10 http://127.0.0.1:9179/api/rr/status | python3 -m json.tool 2>/dev/null | head -25
echo '--- journal peer errors ---'
journalctl -u bgp-agent -n 50 --no-pager 2>/dev/null | grep -iE '{PEER}|{SPOOF}|passive|error|Active|estab|Can.t find' | tail -20
""",
            timeout=180,
        )
    )

    print("\n" + "=" * 60)
    print(f"Linux 201 @ {H201}")
    print("=" * 60)
    print(
        run(
            H201,
            pw,
            f"""
set -x
PEER={PEER}
SPOOF={SPOOF}
echo '--- interfaces 152 ---'
ip -br addr | grep -E '152|153|ens' || ip -br addr | head -20
echo '--- listen :179 ---'
ss -tlnp | grep ':179' || echo 'not listening 179'
echo '--- established to 200 side ---'
ss -tnp state established | grep -E ':179|153\\.' | head -15
echo '--- FRR bgp summary ---'
vtysh -c 'show bgp ipv4 unicast summary' 2>/dev/null | head -25 || echo vtysh_fail
echo '--- neighbors toward 153.204 / 152 ---'
vtysh -c 'show running-config' 2>/dev/null | grep -iE 'neighbor|remote-as|update-source|router bgp' | head -40
echo '--- ping 153.204 ---'
ping -c2 -W2 {SPOOF} 2>&1 || true
""",
            timeout=120,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
