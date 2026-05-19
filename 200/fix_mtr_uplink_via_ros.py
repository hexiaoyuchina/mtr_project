#!/usr/bin/env python3
"""201→200(ens192) 转发公网探测时，走 ens224/ROS（table 2103），勿走 ens160 管理口。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H200, H201 = "10.133.151.200", "10.133.151.201"
DST = "210.73.209.82"
SRC = "10.133.152.204"
IFACE = "ens192"
UPLINK = "ens224"
RR = "10.133.153.204"
RR_LOCAL = "10.133.153.200"
TABLE = "2103"
RULE_PREF = "43"


def load_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def run(host: str, script: str, pw: str, timeout: int = 120) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    rr = os.environ.get("RR_ADDR", RR).strip()
    uplink = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", UPLINK).strip()
    if not pw:
        print("need MTR_OP_SSH_PASSWORD", file=sys.stderr)
        return 2

    on_200 = f"""
set -e
sysctl -w net.ipv4.ip_forward=1
for i in all default {IFACE} {uplink}; do
  sysctl -w net.ipv4.conf.$i.rp_filter=2 2>/dev/null || true
done

ip link set {IFACE} up
ip link set {uplink} up
ip addr show dev {uplink} | grep -q '{RR_LOCAL}/' || ip addr add {RR_LOCAL}/32 dev {uplink} 2>/dev/null || true

# 去掉经管理口去公网的错误静态（setup_201_via_200_mtr 曾写入）
ip route del {DST}/32 via 10.133.151.254 dev ens160 2>/dev/null || true
ip route del 210.73.209.0/24 via 10.133.151.254 dev ens160 2>/dev/null || true
ip route del {DST}/32 via 10.133.153.254 dev {uplink} 2>/dev/null || true

# 从 ens192 进来的转发/探测：查 ROS 侧路由表（与 remote-network-prereq 的 2103 一致）
ip rule del pref {RULE_PREF} 2>/dev/null || true
ip rule del pref {RULE_PREF} iif {IFACE} 2>/dev/null || true
ip rule add pref {RULE_PREF} iif {IFACE} lookup {TABLE}
ip rule del pref 44 from 10.133.152.0/24 2>/dev/null || true
ip rule add pref 44 from 10.133.152.0/24 lookup {TABLE}

ip route replace table {TABLE} 10.133.152.0/24 dev {IFACE} scope link
ip route replace table {TABLE} {SRC}/32 dev {IFACE} scope link
ip route replace table {TABLE} {rr}/32 dev {uplink} scope link src {RR_LOCAL}
ip route replace table {TABLE} default via {rr} dev {uplink} src {RR_LOCAL}

echo '--- rule ---'
ip rule list | grep -E '{RULE_PREF}|^44:'
echo '--- table {TABLE} ---'
ip route show table {TABLE}
echo '--- forward path (201 src) ---'
ip route get {DST} from {SRC} iif {IFACE}
echo '--- main (should not be ens160 for above) ---'
ip route get {DST} 2>&1 | head -1
"""

    on_201 = f"""
mtr -4 -r -n -m 8 -c 3 -a {SRC} -I {IFACE} {DST} 2>&1
"""

    print("=== Linux 200: uplink via ROS table ===")
    print(run(H200, on_200, pw))
    print("\n=== Linux 201: mtr ===")
    out = run(H201, on_201, pw, timeout=90)
    print(out)
    if "10.133.151.254" in out.splitlines()[4:6] if len(out.splitlines()) > 5 else "":
        print("WARN: hop 2 still 151.254", file=sys.stderr)
        return 1
    if rr in out or "153." in out:
        print("fix_mtr_uplink_via_ros_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
