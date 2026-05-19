#!/usr/bin/env python3
"""仅 ens192 下游：不碰 ens160 管理口、ens224 上游 RR。"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H200, H201 = "10.133.151.200", "10.133.151.201"
VRF, PEER, SPOOF, IFACE = "vbgp10133153204", "10.133.152.204", "10.133.153.204", "ens192"
IV, AS = "iv204", 63199
PEER_MAC, SPOOF_MAC = "00:50:56:af:01:5a", "00:50:56:af:97:a6"


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


def scp_to_200(local_rel: str, remote_sub: str) -> None:
    local = LAB.parent / local_rel
    remote = f"{os.environ.get('MTR_OP_REMOTE_DIR', '/root/mtr_op')}/{remote_sub}"
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H200, username="root", password=os.environ["MTR_OP_SSH_PASSWORD"], timeout=45)
    sftp = c.open_sftp()
    sftp.put(str(local), remote)
    sftp.close()
    c.close()
    print(f"  uploaded {local_rel} -> {remote}")


def main() -> int:
    load_env()
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")

    print("=== 同步 OP 代码（DNAT ens192 修复）===")
    scp_to_200("service/app/bgp_ipvlan_reconcile.py", "app/bgp_ipvlan_reconcile.py")
    scp_to_200("service/app/main.py", "app/main.py")
    ssh(H200, "systemctl restart mtr-op; sleep 4; systemctl is-active mtr-op", timeout=60)

    print("\n=== OP: 仅下游 ARP/BGP（ens192）===")
    api("PUT", "/api/arp-spoof/settings", {"arp_spoof_enabled": True})
    api("POST", "/api/arp-spoof/targets", {
        "spoof_gateway_ip": SPOOF,
        "satellite_vrf": VRF,
        "egress_iface": IFACE,
        "enabled": True,
        "policy_mode": "gateway_only",
    })
    api("DELETE", f"/api/bgp/neighbors/default/{PEER}")
    api("DELETE", f"/api/bgp/neighbors/{VRF}/{PEER}")
    c, j = api("POST", "/api/bgp/neighbors", {
        "vrf": VRF,
        "neighbor_ip": PEER,
        "remote_as": AS,
        "role": "downstream",
        "source_ip": SPOOF,
        "create_kernel_vrf_if_missing": True,
    }, timeout=180)
    print("BGP add", c, j.get("session_state") if isinstance(j, dict) else j)

    print("\n=== 200: 仅 ens192 / iv204 / vbgp ===")
    print(
        ssh(
            H200,
            f"""
set -e
export MTR_BGP_PEER_NEIGH_MAC_{PEER.replace('.', '_')}={PEER_MAC}
export MTR_BGP_IPVLAN_AUTO=1
export MTR_BGP_IPVLAN_BASE_IFACE=ens192
export MTR_BGP_RR_UPLINK_IFACE=ens224
export MTR_BGP_SAT_DNAT_IIF=1
export RR_ADDR=10.133.153.204
export MTR_OP_REMOTE_DIR={remote}

# 不碰 ens160 / ens224
ip link set ens192 up
sysctl -w net.ipv4.ip_nonlocal_bind=1 net.ipv4.tcp_l3mdev_accept=1 \\
  net.ipv4.conf.ens192.rp_filter=0 net.ipv4.conf.iv204.rp_filter=0

cd {remote} && ./venv/bin/python3 -c "
from pathlib import Path
from app import bgp_ipvlan_reconcile
db = Path('{remote}/data.db')
print('should_dnat', bgp_ipvlan_reconcile.should_satellite_dnat_spoof_ip('{SPOOF}', 'ens192'))
print(bgp_ipvlan_reconcile.reconcile_vrf_from_op_database(db, '{VRF}', peer_ip='{PEER}'))
print(bgp_ipvlan_reconcile.reconcile_satellite_dnat(db))
"

ip neigh replace {PEER} lladdr {PEER_MAC} dev {IV} nud permanent
ip link set {IV} up

curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove \\
  -H 'Content-Type: application/json' -d '{{"address":"{PEER}","vrf":"default"}}' || true
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove \\
  -H 'Content-Type: application/json' -d '{{"address":"{PEER}","vrf":"{VRF}"}}' || true
sleep 2
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/add \\
  -H 'Content-Type: application/json' \\
  -d '{{"address":"{PEER}","remote_as":{AS},"role":"downstream","vrf":"{VRF}","local_address":"{SPOOF}","bind_interface":"{IV}","passive_mode":true}}'
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle \\
  -H 'Content-Type: application/json' -d '{{"address":"{PEER}","vrf":"{VRF}","enabled":true}}'

pkill -f mtr_spoof_nfqueue 2>/dev/null || true
sleep 1
cd {remote} && nohup ./venv/bin/python3 mtr_spoof_nfqueue.py --op-db {remote}/data.db >>/tmp/mtr_spoof_nfqueue.log 2>&1 &

echo '--- nft dnat ---'
nft list chain inet mtr_bgp_sat_dnat prerouting 2>/dev/null | grep -E '153.204|ens192' || true
echo '--- ping ens192 path ---'
ping -c2 -W2 -I {SPOOF} {PEER} || true
""",
            timeout=240,
        )
    )

    print("\n=== 201: ens192 回程（仅路由/邻居，不改 FRR 配置）===")
    print(
        ssh(
            H201,
            f"""
ip link set ens192 up
sysctl -w net.ipv4.conf.ens192.rp_filter=0
ip route replace 10.133.153.0/24 dev ens192
ip neigh replace {SPOOF} lladdr {SPOOF_MAC} dev ens192 nud permanent
vtysh -c 'clear ip bgp {SPOOF}' 2>/dev/null || true
""",
            timeout=60,
        )
    )

    print("\n等待 40s…")
    time.sleep(40)
    print(
        ssh(
            H200,
            f"""
ss -tnp | grep -E ':179|:183' | grep {PEER} || echo no_tcp
nft list chain inet mtr_bgp_sat_dnat prerouting 2>/dev/null | head -8
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='{PEER}':
    print(n.get('vrf'), n.get('state'), 'pfx', n.get('pfx_rcd'), 'passive', n.get('passive_mode'))
"
""",
            timeout=60,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
