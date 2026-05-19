#!/usr/bin/env python3
"""排查 201 ping 10.133.152.249（ARP 引流 ens192 gateway_only）。"""
import json
import os
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

pw = os.environ["MTR_OP_SSH_PASSWORD"]
H200, H201 = "10.133.151.200", "10.133.151.201"
TARGET = "10.133.152.249"
SRC = "10.133.152.204"
IFACE = "ens192"


def ssh(host: str, script: str, timeout: int = 90) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    return (o.read() + e.read()).decode("utf-8", "replace")


def api(path: str):
    with urllib.request.urlopen(
        f"http://{H200}:8808{path}", timeout=15, headers={"Accept": "application/json"}
    ) as r:
        return json.loads(r.read().decode())


print("=== OP ARP 配置 ===")
try:
    print("settings:", api("/api/arp-spoof/settings"))
    for t in api("/api/arp-spoof/targets"):
        if TARGET in str(t) or "249" in str(t.get("spoof_gateway_ip", "")):
            print("target:", t)
    print("all targets count:", len(api("/api/arp-spoof/targets")))
except Exception as ex:
    print("api_err:", ex)

print("\n=== 200: 地址 / 邻居 / 进程 ===")
print(
    ssh(
        H200,
        f"""
echo '--- addr ens192 ---'
ip -br addr show {IFACE}
ip addr show dev {IFACE} | grep -E '152\\.|inet '
echo '--- route get {TARGET} ---'
ip route get {TARGET} 2>&1
echo '--- neigh {TARGET} ---'
ip neigh show {TARGET} dev {IFACE} 2>/dev/null || ip neigh show {TARGET}
echo '--- arp daemon ---'
ps aux | grep arp_spoof | grep -v grep || echo '(no arp_spoof_daemon)'
systemctl is-active mtr-op 2>/dev/null || true
echo '--- nft echo accept for 249? ---'
nft list chain ip mtr_spoof prerouting 2>/dev/null | grep -F '{TARGET}' || echo '(no nft rule or no mtr_spoof)'
echo '--- sysctl ---'
sysctl net.ipv4.conf.{IFACE}.rp_filter net.ipv4.icmp_echo_ignore_all 2>/dev/null
""",
    )
)

print("\n=== 201: neigh / ping / route ===")
print(
    ssh(
        H201,
        f"""
echo '--- route get {TARGET} from {SRC} ---'
ip route get {TARGET} from {SRC} iif {IFACE} 2>&1
echo '--- neigh {TARGET} on {IFACE} ---'
ip neigh show {TARGET} dev {IFACE} 2>/dev/null || ip neigh show {TARGET}
echo '--- MAC ens192 on 201 ---'
cat /sys/class/net/{IFACE}/address
echo '--- ping 3 ---'
ping -c 3 -W 2 -a {SRC} -I {IFACE} {TARGET} 2>&1 || true
""",
        timeout=30,
    )
)

print("\n=== 200: tcpdump 5s while 201 pings ===")
# start ping from 201 in background via 200 ssh to 201... run tcpdump on 200
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(H200, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
_, o, e = c.exec_command(
    f"timeout 6 tcpdump -ni {IFACE} -c 12 'host {TARGET} or (arp and host {TARGET})' 2>&1",
    timeout=15,
)
import threading
import time

def ping201():
    time.sleep(0.5)
    ssh(H201, f"ping -c 4 -W 1 -a {SRC} -I {IFACE} {TARGET} >/dev/null 2>&1", timeout=20)


threading.Thread(target=ping201).start()
print((o.read() + e.read()).decode())
c.close()
