#!/usr/bin/env python3
"""恢复 vbgp233 / vbgp204 下游 BGP；清理与 ARP 冲突的 pref 44 策略。"""
from __future__ import annotations

import os
import time
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
PEER = "10.133.152.204"
PEER_MAC = "00:50:56:af:01:5a"
PEERS = (
    ("vbgp10133152233", "10.133.152.233", "iv233"),
    ("vbgp10133153204", "10.133.153.204", "iv204"),
)


def load_env() -> str:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    return os.environ["MTR_OP_SSH_PASSWORD"]


def run(host: str, pw: str, script: str, timeout: int = 180) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=45)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    return (o.read() + e.read()).decode("utf-8", "replace")


def main() -> int:
    pw = load_env()
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.133.151.200", username="root", password=pw, timeout=45)
    sftp = c.open_sftp()
    sftp.put(str(LAB / "remote-network-prereq.sh"), f"{remote}/remote-network-prereq.sh")
    sftp.put(str(LAB.parent / "service" / "app" / "main.py"), f"{remote}/app/main.py")
    sftp.close()
    c.close()

    print("=== 200: 策略 + ipvlan + BGP ===")
    vrf_blocks = ""
    for vrf, spoof, iv in PEERS:
        vrf_blocks += f"""
export MTR_BGP_PEER_NEIGH_MAC_{PEER.replace('.', '_')}={PEER_MAC}
cd {remote} && ./venv/bin/python3 -c "
from pathlib import Path
from app import bgp_ipvlan_reconcile
db = Path('{remote}/data.db')
print(bgp_ipvlan_reconcile.reconcile_vrf_from_op_database(db, '{vrf}', peer_ip='{PEER}'))
print(bgp_ipvlan_reconcile.reconcile_satellite_dnat(db))
"
ip neigh replace {PEER} lladdr {PEER_MAC} dev {iv} nud permanent
ip link set {iv} up
ip route get {PEER} from {spoof} 2>&1
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove -H 'Content-Type: application/json' \\
  -d '{{"address":"{PEER}","vrf":"{vrf}"}}' || true
sleep 1
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/add -H 'Content-Type: application/json' \\
  -d '{{"address":"{PEER}","remote_as":63199,"role":"downstream","vrf":"{vrf}","local_address":"{spoof}","bind_interface":"{iv}","passive_mode":false}}'
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle -H 'Content-Type: application/json' \\
  -d '{{"address":"{PEER}","vrf":"{vrf}","enabled":true}}'
"""

    print(
        run(
            "10.133.151.200",
            pw,
            f"""
set -e
export RR_ADDR=10.133.153.204 ROUTER_ID=10.133.153.200 MTR_BGP_RR_UPLINK_IFACE=ens224
export MTR_BGP_IPVLAN_BASE_IFACE=ens192 MTR_BGP_SAT_DNAT_IIF=1
bash {remote}/remote-network-prereq.sh
ip link set ens192 up
sysctl -w net.ipv4.ip_nonlocal_bind=1 net.ipv4.tcp_l3mdev_accept=1
ip -4 rule show | grep -E '43:|44:|45:|233|204'
{vrf_blocks}
systemctl restart mtr-op
sleep 3
""",
            timeout=300,
        )
    )

    print("\n=== 201: ens192 回程 ===")
    print(
        run(
            "10.133.151.201",
            pw,
            """
ip link set ens192 up
ip route del 10.133.153.0/24 via 10.133.152.200 2>/dev/null || true
ip route replace 10.133.152.233/32 dev ens192 scope link
ip route replace 10.133.153.204/32 dev ens192 scope link
ip route replace 10.133.152.0/24 dev ens192 scope link
sysctl -w net.ipv4.conf.ens192.rp_filter=0
""",
            timeout=30,
        )
    )

    time.sleep(25)
    print(
        run(
            "10.133.151.200",
            pw,
            """
ss -tnp | grep 152.204 | head -8
curl -sf --max-time 8 http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='10.133.152.204':
    print(n.get('vrf'), n.get('state'), 'rcvd', n.get('pfx_rcd'))
"
curl -sf --max-time 5 http://127.0.0.1:9179/api/rr/status | python3 -c "
import json,sys
p=json.load(sys.stdin).get('rx_status',{}).get('rr_peers',[{}])[0]
print('rr', p.get('state'), p.get('pfx_rcd'))
"
""",
            timeout=40,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
