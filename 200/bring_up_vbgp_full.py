#!/usr/bin/env python3
"""按 OP 创建步骤调通 vbgp10133153204 / ens192 / 153.204 -> 152.204。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H200, H201 = "10.133.151.200", "10.133.151.201"
VRF, PEER, SPOOF, IFACE = "vbgp10133153204", "10.133.152.204", "10.133.153.204", "ens192"
IV = "iv204"
AS = 63199
# 201 ens192 MAC（现场已确认）
PEER_MAC = "00:50:56:af:01:5a"
# 200 iv204 MAC
SPOOF_MAC = "00:50:56:af:97:a6"


def load_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def api(method: str, path: str, body: dict | None = None, timeout: int = 120):
    url = f"http://{os.environ['MTR_OP_HOST']}:{os.environ.get('MTR_OP_PORT','8808')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw[:500]}


def ssh(host: str, script: str, timeout: int = 180) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=os.environ["MTR_OP_SSH_PASSWORD"], timeout=45, banner_timeout=60)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    load_env()
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")

    print("=== OP: ARP + BGP（幂等）===")
    api("PUT", "/api/arp-spoof/settings", {"arp_spoof_enabled": True})
    c, j = api("POST", "/api/arp-spoof/targets", {
        "spoof_gateway_ip": SPOOF,
        "satellite_vrf": VRF,
        "egress_iface": IFACE,
        "enabled": True,
        "policy_mode": "gateway_only",
        "note": "BGPSAT",
    })
    print("ARP", c, j.get("id") if isinstance(j, dict) else j)
    api("DELETE", f"/api/bgp/neighbors/default/{PEER}")
    c, j = api("POST", "/api/bgp/neighbors", {
        "vrf": VRF,
        "neighbor_ip": PEER,
        "remote_as": AS,
        "role": "downstream",
        "source_ip": SPOOF,
        "create_kernel_vrf_if_missing": True,
    }, timeout=180)
    print("BGP add", c, j.get("session_state") if isinstance(j, dict) else j)

    print("\n=== 200: 网络 + ipvlan + 邻居 ===")
    print(
        ssh(
            H200,
            f"""
set -e
export MTR_BGP_PEER_NEIGH_MAC_{PEER.replace('.','_')}={PEER_MAC}
export MTR_BGP_PEER_NEIGH_MAC={PEER_MAC}
export MTR_OP_REMOTE_DIR={remote}
export MTR_OP_DB={remote}/data.db
export MTR_BGP_IPVLAN_AUTO=1
export MTR_BGP_IPVLAN_BASE_IFACE=ens192
export ROUTER_ID=10.133.153.200
export RR_ADDR=10.133.153.204

ip link set ens192 up
ip link set ens224 up
ip addr add 10.133.153.200/32 dev ens224 2>/dev/null || true
bash {remote}/remote-network-prereq.sh 2>/dev/null || true
sysctl -w net.ipv4.ip_nonlocal_bind=1 net.ipv4.tcp_l3mdev_accept=1 \\
  net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0 \\
  net.ipv4.conf.ens192.rp_filter=0 net.ipv4.conf.iv204.rp_filter=0

cd {remote} && ./venv/bin/python3 -c "
from pathlib import Path
from app import bgp_ipvlan_reconcile
db = Path('{remote}/data.db')
print('ipvlan', bgp_ipvlan_reconcile.reconcile_vrf_from_op_database(db, '{VRF}', peer_ip='{PEER}'))
"

ip neigh replace {PEER} lladdr {PEER_MAC} dev {IV} nud permanent
ip link set {IV} up
ip link set {VRF} up

# 删 default 重复（sqlite + agent）
sqlite3 {remote}/data.db "DELETE FROM bgp_neighbor_meta WHERE vrf='default' AND neighbor_ip='{PEER}';" 2>/dev/null || true
curl -sf -X DELETE http://127.0.0.1:8808/api/bgp/neighbors/default/{PEER} || true
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove \\
  -H 'Content-Type: application/json' -d '{{"address":"{PEER}","vrf":"default"}}' || true

curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove \\
  -H 'Content-Type: application/json' -d '{{"address":"{PEER}","vrf":"{VRF}"}}' || true
sleep 2
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/add \\
  -H 'Content-Type: application/json' \\
  -d '{{"address":"{PEER}","remote_as":{AS},"role":"downstream","vrf":"{VRF}","local_address":"{SPOOF}","bind_interface":"{IV}"}}'
echo
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle \\
  -H 'Content-Type: application/json' -d '{{"address":"{PEER}","vrf":"{VRF}","enabled":true}}'
echo

curl -sf -X POST http://127.0.0.1:9179/api/rr/config \\
  -H 'Content-Type: application/json' \\
  -d '{{"address":"{SPOOF}","remote_as":{AS},"local_address":"10.133.153.200"}}'
curl -sf -X POST http://127.0.0.1:9179/api/gobgp/unfreeze
curl -sf -X POST http://127.0.0.1:8808/api/gobgp/unfreeze
echo

pkill -f mtr_spoof_nfqueue 2>/dev/null || true

echo '--- ping ---'
ping -c2 -W2 -I {SPOOF} {PEER} || true
ip vrf exec {VRF} ping -c2 -W2 {PEER} || true
""",
            timeout=240,
        )
    )

    print("\n=== 201: 静态 ARP + 清 BGP 会话 ===")
    print(
        ssh(
            H201,
            f"""
ip link set ens192 up
ip neigh replace {SPOOF} lladdr {SPOOF_MAC} dev ens192 nud permanent
vtysh -c 'clear ip bgp {SPOOF}' 2>/dev/null || true
sleep 3
vtysh -c 'show bgp summary' 2>/dev/null | grep {SPOOF} || true
""",
            timeout=60,
        )
    )

    print("\n等待 35s BGP 建连…")
    time.sleep(35)

    print(
        ssh(
            H200,
            f"""
echo '--- TCP ---'
ss -tnp | grep {PEER} || echo no_tcp
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='{PEER}':
    print(n.get('vrf'), n.get('state'), 'pfx_rcd', n.get('pfx_rcd'), 'la', n.get('local_address'))
"
curl -sf http://127.0.0.1:8808/api/bgp/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin):
  if n.get('neighbor_ip')=='{PEER}':
    print(n.get('vrf'), n.get('session_state'), n.get('source_ip'))
"
""",
            timeout=60,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
