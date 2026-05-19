#!/usr/bin/env python3
"""仅 Linux 200：ens192 / vbgp10133153204 / 153.204 冒充 下游 BGP 配置诊断。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

H200 = "10.133.151.200"
VRF = "vbgp10133153204"
PEER = "10.133.152.204"
SPOOF = "10.133.153.204"


def load_env() -> str:
    for line in Path(__file__).resolve().parent.joinpath("lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def main() -> int:
    pw = load_env()
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
    script = f"""
set -x
VRF={VRF}
PEER={PEER}
SPOOF={SPOOF}
REMOTE={remote}

echo '========== 1) 物理/ipvlan =========='
ip -br link show ens192 ens224 iv204 2>/dev/null || true
ip -br addr show ens192 iv204 2>/dev/null || true
ip link show master "$VRF" 2>/dev/null || echo 'no master for vrf'
ip -br addr show master "$VRF" 2>/dev/null || true

echo '========== 2) VRF 路由 =========='
ip vrf show "$VRF" 2>/dev/null || true
ip route show vrf "$VRF" 2>/dev/null
ip rule | grep -E '30404|153.204' || true
ip route get "$PEER" vrf "$VRF" 2>/dev/null || true
ip route get "$PEER" from "$SPOOF" vrf "$VRF" 2>/dev/null || true

echo '========== 3) nft DNAT (153.204:179) =========='
nft list table inet mtr_bgp_sat_dnat 2>/dev/null || echo 'NO mtr_bgp_sat_dnat'
nft list table inet mtr_bgp_spoof_rr 2>/dev/null | head -15 || true

echo '========== 4) bgp-agent 监听 =========='
ss -tlnp | grep bgp_agent | head -15
echo '--- 到 PEER 的 TCP ---'
ss -tnp | grep "$PEER" || echo 'no tcp to peer'
ss -tnp state syn-sent | grep "$PEER" || true
ss -tnp state listen | grep -E '1833|1790|179 ' || true

echo '========== 5) Agent 邻居配置 =========='
curl -sf --max-time 15 http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('vrf')=='{VRF}' or (n.get('address')=='{PEER}' and n.get('session')=='tx'):
    print(json.dumps(n,indent=2))
"

echo '========== 6) OP sqlite meta =========='
sqlite3 "$REMOTE/data.db" "SELECT vrf,neighbor_ip,role,source_ip,advertise_routes FROM bgp_neighbor_meta WHERE vrf='{VRF}' OR neighbor_ip='{PEER}';" 2>/dev/null

echo '========== 7) ipvlan reconcile 状态 =========='
cat "$REMOTE/data/.bgp_ipvlan_reconcile.json" 2>/dev/null | python3 -m json.tool 2>/dev/null | head -40 || ls -la "$REMOTE/data/.bgp_ipvlan_reconcile.json" 2>/dev/null

echo '========== 8) gobgp / journal (vbgp) =========='
journalctl -u bgp-agent -n 80 --no-pager 2>/dev/null | grep -iE '{VRF}|{PEER}|{SPOOF}|ens192|1833|1790|bind|passive|error|fail|estab' | tail -25

echo '========== 9) 从 200 主动连 152.204:179 (源 153.204) =========='
ip vrf exec "$VRF" nc -zv -w3 "$PEER" 179 2>&1 || true
timeout 3 bash -c "echo | ip vrf exec $VRF nc -s $SPOOF $PEER 179" 2>&1 || true
"""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H200, username="root", password=pw, timeout=45, allow_agent=False, look_for_keys=False, banner_timeout=45)
    _, o, e = c.exec_command("bash -se", timeout=120)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    print((o.read() + e.read()).decode("utf-8", "replace"))
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
