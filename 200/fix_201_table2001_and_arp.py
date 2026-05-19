#!/usr/bin/env python3
"""修复 201 空表 2001；启动 200 ARP 守护；触发 ipvlan reconcile。"""
import json
import os
import urllib.request
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
LAB = Path(__file__).resolve().parent
for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

pw = os.environ["MTR_OP_SSH_PASSWORD"]
H200, H201 = "10.133.151.200", "10.133.151.201"
REMOTE = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")


def ssh(host: str, script: str, timeout: int = 120) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    return (o.read() + e.read()).decode("utf-8", "replace")


print("=== 201: 恢复 table 2001 ===")
policy = (ROOT / "scripts" / "linux201_src152_policy_route.sh").read_text(encoding="utf-8")
print(ssh(H201, policy))

print("\n=== 200: ARP 守护 + reconcile ===")
print(
    ssh(
        H200,
        f"""
set -e
export MTR_OP_DB={REMOTE}/data.db
export MTR_BGP_IPVLAN_AUTO=1
export MTR_BGP_IPVLAN_BASE_IFACE=ens192
export MTR_BGP_SAT_DNAT_IIF=1
export RR_ADDR=10.133.153.204

pkill -f arp_spoof_daemon.py 2>/dev/null || true
nohup /usr/bin/python3 {REMOTE}/scripts/arp_spoof_daemon.py --op-db $MTR_OP_DB >>/tmp/arp_spoof_daemon.log 2>&1 &
sleep 1
ps aux | grep arp_spoof_daemon | grep -v grep | head -1

cd {REMOTE}
./venv/bin/python3 - <<'PY'
from pathlib import Path
from app import bgp_ipvlan_reconcile, arp_spoof_assign
db = Path("{REMOTE}/data.db")
print("arp_assign", arp_spoof_assign.reconcile_from_op_database(db))
print("ipvlan", bgp_ipvlan_reconcile.reconcile_from_op_database(db))
PY
""",
    )
)

print("\n=== OP: ipvlan reconcile API ===")
req = urllib.request.Request(
    f"http://{H200}:8808/api/bgp/ipvlan-satellites/reconcile",
    method="POST",
    headers={"Accept": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        print(json.loads(r.read().decode())[:800] if False else r.read().decode()[:1200])
except Exception as ex:
    print("api:", ex)

print("\n=== 验证 ping / BGP ===")
print(
    ssh(
        H201,
        """
ip route show table 2001
ip route get 10.133.152.249 from 10.133.152.204 iif ens192
ping -c3 -W2 -a 10.133.152.204 -I ens192 10.133.152.249
ping -c2 -W2 -a 10.133.152.204 -I ens192 10.133.153.204
""",
        timeout=40,
    )
)
print(
    ssh(
        H200,
        "curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c \"import json,sys;[print(n.get('vrf'),n.get('address'),n.get('state'),n.get('local_address')) for n in json.load(sys.stdin).get('neighbors',[])]\"",
        timeout=30,
    )
)
