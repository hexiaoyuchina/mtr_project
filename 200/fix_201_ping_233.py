#!/usr/bin/env python3
"""201: ping -I ens192 10.133.152.233；检查并修复 200 ARP/ipvlan。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
LAB = Path(__file__).resolve().parent
for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
if not pw:
    print("need MTR_OP_SSH_PASSWORD", file=sys.stderr)
    sys.exit(2)

H200, H201 = "10.133.151.200", "10.133.151.201"
REMOTE = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
TARGET, GW, IFACE = "10.133.152.233", "10.133.152.200", "ens192"
MAC200 = "00:50:56:af:97:a6"


def ssh(host: str, script: str, timeout: int = 120) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    return (o.read() + e.read()).decode("utf-8", "replace")


def upload_arp_daemon() -> None:
    local = ROOT / "scripts" / "arp_spoof_daemon.py"
    if not local.is_file():
        return
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H200, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    try:
        sftp.stat(f"{REMOTE}/scripts")
    except OSError:
        sftp.mkdir(f"{REMOTE}/scripts")
    sftp.put(str(local), f"{REMOTE}/scripts/arp_spoof_daemon.py")
    sftp.close()
    c.close()


print("=== Linux 200：链路 / iv233 / ARP 守护 ===")
upload_arp_daemon()
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

sysctl -w net.ipv4.ip_forward=1 net.ipv4.conf.all.rp_filter=0 \\
  net.ipv4.conf.ens192.rp_filter=0 net.ipv4.conf.iv233.rp_filter=0 \\
  net.ipv4.conf.ens192.proxy_arp=1

ip link set ens192 up

# 卫星 IP 策略路由（pref 须小于 43/44，否则 table 2103 吞掉本机 iv*）
for p in 37 38 39 40 41 42; do ip rule del pref $p 2>/dev/null || true; done
ip rule add pref 38 to {TARGET} lookup 30433
ip rule add pref 39 to 10.133.153.204 lookup 30404
ip rule add pref 40 from {TARGET} lookup 30433
ip rule add pref 41 from 10.133.153.204 lookup 30404
ip route replace table 2103 {TARGET}/32 dev iv233 scope link
ip route replace table 2103 10.133.153.204/32 dev iv204 scope link
export MTR_BGP_PEER_NEIGH_MAC_10_133_152_204=00:50:56:af:01:5a
ip neigh replace 10.133.152.204 lladdr 00:50:56:af:01:5a dev iv233 nud permanent
ip neigh del {TARGET} dev iv233 2>/dev/null || true
sysctl -w net.ipv4.conf.iv233.accept_local=1 net.ipv4.conf.all.send_redirects=0
ip addr show dev ens192 | grep -q '{GW}/' || ip addr add {GW}/24 dev ens192 2>/dev/null || true

cd {REMOTE}
./venv/bin/python3 - <<'PY'
from pathlib import Path
from app import arp_spoof_assign, bgp_ipvlan_reconcile
db = Path("{REMOTE}/data.db")
print("ipvlan", bgp_ipvlan_reconcile.reconcile_from_op_database(db))
print("arp_host", arp_spoof_assign.reconcile_from_op_database(db))
PY

pkill -f arp_spoof_daemon.py 2>/dev/null || true
sleep 1
cd {REMOTE}
nohup ./venv/bin/python3 scripts/arp_spoof_daemon.py --op-db $MTR_OP_DB >>/tmp/arp_spoof_daemon.log 2>&1 &
sleep 3

echo '--- 200 addr iv233 ---'
ip -br addr show iv233 2>/dev/null || echo 'no iv233'
ip -br addr show ens192
echo '--- arp daemon ---'
pgrep -af arp_spoof_daemon | head -1 || tail -3 /tmp/arp_spoof_daemon.log
echo '--- local ping 233 from iv233 ---'
ping -c2 -W2 -I {TARGET} 10.133.152.204 2>&1 | tail -3 || true
echo '--- tcpdump 3s GARP ---'
timeout 3 tcpdump -ni ens192 -c 6 'arp and host {TARGET}' 2>&1 | tail -8 || true
""",
        timeout=90,
    )
)

policy = (ROOT / "scripts" / "linux201_src152_policy_route.sh").read_text(encoding="utf-8")
# ping -I ens192 走主表到 152.0/24；仍恢复 table 2001 与网关邻居
policy += f"""
sysctl -w net.ipv4.conf.ens192.rp_filter=0
ip neigh replace {GW} lladdr {MAC200} dev {IFACE} nud permanent
ip link set {IFACE} up
"""

print("\n=== Linux 201：路由 + 邻居 + ping ===")
print(ssh(H201, policy, timeout=60))
print(
    ssh(
        H201,
        f"""
echo '--- route get {TARGET} ---'
ip route get {TARGET} oif {IFACE} 2>&1 || ip route get {TARGET} 2>&1
echo '--- neigh ---'
ip neigh show {TARGET} dev {IFACE} 2>/dev/null || ip neigh show {TARGET} || true
ip neigh show {GW} dev {IFACE} 2>/dev/null || true
echo '--- ping ---'
ping -c4 -W2 -I {IFACE} {TARGET}
""",
        timeout=40,
    )
)

import threading
import time

_cap: list[str] = []


def _cap_on_200() -> None:
    _cap.append(
        ssh(
            H200,
            f"timeout 7 tcpdump -ni ens192 -c 10 'host {TARGET}' 2>&1",
            timeout=12,
        )
    )


def _ping_on_201() -> None:
    time.sleep(0.4)
    ssh(H201, f"ping -c4 -W1 -I {IFACE} {TARGET}", timeout=20)


print("\n=== 201 ping + 200 tcpdump ===")
t0 = threading.Thread(target=_cap_on_200)
t1 = threading.Thread(target=_ping_on_201)
t0.start()
t1.start()
t0.join()
t1.join()
if _cap:
    print(_cap[0])
