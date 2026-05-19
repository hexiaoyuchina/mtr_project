#!/usr/bin/env python3
"""登录 RouterOS 151.210 (admin) 验证 RR 是否收到 203.0.113.201/32 from 153.200。"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
ROS_MGMT = "10.133.151.210"
OP_HOST = "10.133.151.200"
RR_BGP = "10.133.153.204"
PEER = "10.133.153.200"
PREFIX = "203.0.113.201"
PW = "1234qwer"


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def ros(cmd: str, pw: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        ROS_MGMT,
        username="admin",
        password=pw,
        timeout=25,
        allow_agent=False,
        look_for_keys=False,
    )
    _, o, e = c.exec_command(cmd if cmd.startswith("/") else f"/{cmd}", timeout=90)
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def op_agent_rr(pw: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(OP_HOST, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    cmd = f"""
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "import json,sys;d=json.load(sys.stdin);nb=d if isinstance(d,list) else d.get('neighbors',[]);print([{{'pfx_adv':n.get('pfx_adv'),'pfx_rcd':n.get('pfx_rcd'),'state':n.get('state')}} for n in nb if n.get('address')=='{RR_BGP}'])"
curl -sf http://127.0.0.1:8808/api/bgp/neighbors/gobgp-rr/{RR_BGP}/advertise/status
"""
    _, o, e = c.exec_command(cmd, timeout=45)
    out = o.read().decode() + e.read().decode()
    c.close()
    return out


def main() -> int:
    load_lab_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", PW)

    print(f"RouterOS 管理 {ROS_MGMT} (admin)  BGP 对端 {PEER} / 冒充 {RR_BGP}")
    print(f"查前缀 {PREFIX}/32\n")

    print("=== A) 200 控制面 Agent ===")
    print(op_agent_rr(pw))

    print("=== B) ROS BGP peer（连 153.200）===")
    for cmd in [
        '/routing bgp peer print detail where remote-address=10.133.153.200',
        '/routing bgp peer print stats where remote-address=10.133.153.200',
        '/routing bgp peer print detail',
    ]:
        print(f">>> {cmd}")
        print(ros(cmd, pw))

    print(f"=== C) ROS 路由表是否含 {PREFIX} ===")
    for cmd in [
        f'/routing route print where dst-address={PREFIX}/32',
        f'/routing route print detail where dst-address={PREFIX}/32',
        f'/ip route print where dst-address={PREFIX}/32',
        '/routing bgp advertisement print',
    ]:
        print(f">>> {cmd}")
        out = ros(cmd, pw)
        print(out if out.strip() else "(empty)")

    print("=== D) ROS 从 peer 收到的 BGP 前缀抽样 ===")
    print(
        ros(
            '/routing bgp route print count-only',
            pw,
        )
    )
    print(ros('/routing bgp route print where dst-address~"203.0.113"', pw))

    print("\n========== 结论 ==========")
    peer_out = ros('/routing bgp peer print stats where remote-address=10.133.153.200', pw)
    route_out = ros(f'/routing route print where dst-address={PREFIX}/32', pw)
    low_peer = peer_out.lower()
    if PREFIX in route_out and "dst-address" in route_out:
        print(f"ROS 路由表 **有** {PREFIX}/32 → RR 侧已收到该前缀")
    elif "0" in route_out and "routes" in route_out.lower() and PREFIX not in route_out:
        print(f"ROS 路由表 **无** {PREFIX}/32")
    else:
        print(f"ROS 路由查询结果见上（无 dst-address 行则视为未收到）")

    if "prefix-count" in low_peer or "prefix" in low_peer:
        for line in peer_out.splitlines():
            if any(k in line.lower() for k in ("prefix", "received", "sent", "state")):
                print("  peer:", line.strip())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
