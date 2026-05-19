#!/usr/bin/env python3
"""201 经 ens192 -> 200(10.133.152.200) 访问 210.73.209.82，供 mtr -a 152.204 -I ens192。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H200, H201 = "10.133.151.200", "10.133.151.201"
GW200 = "10.133.152.200"
SRC201 = "10.133.152.204"
DST = "210.73.209.82"
IFACE = "ens192"
MAC200 = "00:50:56:af:97:a6"
PW = os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def load_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def run(host: str, script: str, timeout: int = 120) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=PW, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", PW)
    if not pw:
        print("need MTR_OP_SSH_PASSWORD", file=sys.stderr)
        return 2

    def run_host(host: str, script: str, timeout: int = 120) -> str:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
        _, o, e = c.exec_command("bash -se", timeout=timeout)
        o.channel.send(script.encode())
        o.channel.shutdown_write()
        out = (o.read() + e.read()).decode("utf-8", "replace")
        c.close()
        return out

    rr = os.environ.get("RR_ADDR", "10.133.153.204")
    uplink = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "ens224")

    print("=== 200: ens192 + 转发 + 回程 ===")
    print(
        run_host(
            H200,
            f"""
set -e
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv4.conf.all.rp_filter=0
sysctl -w net.ipv4.conf.{IFACE}.rp_filter=0
sysctl -w net.ipv4.conf.all.forwarding=1

ip link set {IFACE} up
# 默认 VRF 在 ens192 上的地址（201 的 BGP 邻居 .200）
ip addr show dev {IFACE} | grep -q '{GW200}/' || ip addr add {GW200}/24 dev {IFACE} 2>/dev/null || true
ip route replace {SRC201}/32 dev {IFACE} scope link
ip route replace 10.133.152.0/24 dev {IFACE} scope link

# 201 经 ens192 转发：查 table 2103，从 {uplink}（ROS/RR）出网，勿走 ens160 管理口
RR_LOCAL=10.133.153.200
ip link set {uplink} up
ip addr show dev {uplink} | grep -q "$RR_LOCAL/" || ip addr add $RR_LOCAL/32 dev {uplink} 2>/dev/null || true
ip route del {DST}/32 via 10.133.151.254 dev ens160 2>/dev/null || true
ip route del 210.73.209.0/24 via 10.133.151.254 dev ens160 2>/dev/null || true
# 勿写 pref 43/44（整段 152.0/24 -> 2103），会盖住卫星 VRF 的 from 冒充源 策略，导致下游 BGP Active。
# 转发 201 流量请用 pref 38-42 的 to/from 单 IP 规则（见下方）+ remote-network-prereq.sh。
ip route replace table 2103 10.133.152.0/24 dev {IFACE} scope link
ip route replace table 2103 {SRC201}/32 dev {IFACE} scope link
ip route replace table 2103 {rr}/32 dev {uplink} scope link src $RR_LOCAL
ip route replace table 2103 default via {rr} dev {uplink} src $RR_LOCAL

# ens192 入站目的为卫星冒充 IP 时须进对应 VRF（先于 pref 43/44，否则 ping/BGP 无 Echo Reply）
for p in 37 38 39 40 41 42; do ip rule del pref $p 2>/dev/null || true; done
ip rule add pref 38 to 10.133.152.233 lookup 30433 2>/dev/null || true
ip rule add pref 39 to 10.133.153.204 lookup 30404 2>/dev/null || true
ip rule add pref 40 from 10.133.152.233 lookup 30433 2>/dev/null || true
ip rule add pref 41 from 10.133.153.204 lookup 30404 2>/dev/null || true
ip route replace table 2103 10.133.152.233/32 dev iv233 scope link 2>/dev/null || true
ip route replace table 2103 10.133.153.204/32 dev iv204 scope link 2>/dev/null || true
export MTR_BGP_PEER_NEIGH_MAC_10_133_152_204=00:50:56:af:01:5a
ip neigh replace 10.133.152.204 lladdr 00:50:56:af:01:5a dev iv233 nud permanent 2>/dev/null || true
sysctl -w net.ipv4.conf.iv233.accept_local=1 net.ipv4.conf.iv204.accept_local=1 2>/dev/null || true

# 允许从 ens192 转发
iptables -C FORWARD -i {IFACE} -j ACCEPT 2>/dev/null || iptables -I FORWARD -i {IFACE} -j ACCEPT
iptables -C FORWARD -o {IFACE} -j ACCEPT 2>/dev/null || iptables -I FORWARD -o {IFACE} -j ACCEPT

# 清除实验性 SNAT/DNAT（会破坏第 2 跳 TE）；TE 改写由 OP te_rewrite_sync 下发 NFQUEUE
iptables -t nat -D POSTROUTING -s {SRC201} -o {uplink} -j SNAT --to-source {GW200} 2>/dev/null || true
iptables -t nat -D PREROUTING -p icmp -m icmp --icmp-type time-exceeded -d {GW200} -j DNAT --to-destination {SRC201} 2>/dev/null || true

echo "--- 200 route get dst ---"
ip route get {DST} 2>&1 | head -1
echo "--- 200 route get from 201 src ---"
ip route get {DST} from {SRC201} iif {IFACE} 2>&1 | head -1
echo "--- ip_forward ---"
sysctl -n net.ipv4.ip_forward
ip -br addr show {IFACE}
""",
        )
    )

    print("\n=== 201: 经 152.200 去 210.73 ===")
    print(
        run_host(
            H201,
            f"""
set -e
sysctl -w net.ipv4.conf.{IFACE}.rp_filter=0
ip link set {IFACE} up

ip neigh replace {GW200} lladdr {MAC200} dev {IFACE} nud permanent
ip route replace {DST}/32 via {GW200} dev {IFACE} onlink
ip route replace 210.73.209.0/24 via {GW200} dev {IFACE} onlink

echo "--- 201 route get (mtr 同源同口) ---"
ip route get {DST} from {SRC201} iif {IFACE}
echo "--- ping 200 ---"
ping -c2 -W2 -I {SRC201} {GW200} || true
echo "--- ping dst (3s) ---"
ping -c2 -W3 -I {SRC201} {DST} || true
""",
        )
    )

    print("\n=== 201 mtr 短测 ===")
    print(
        run_host(
            H201,
            f"mtr -4 -r -n -m 12 -c 3 -a {SRC201} -I {IFACE} {DST} 2>&1 | tail -20",
            timeout=90,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
