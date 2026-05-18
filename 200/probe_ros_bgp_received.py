#!/usr/bin/env python3
"""RouterOS：从各 BGP 邻居收到多少前缀。"""
from __future__ import annotations

import os
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H210 = "10.133.151.210"


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def pw() -> str:
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def ros(cmd: str, timeout: int = 120) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H210, username="admin", password=pw(), timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command(cmd if cmd.startswith("/") else f"/{cmd}", timeout=timeout)
    out = (o.read() + e.read()).decode("utf-8", "replace").strip()
    c.close()
    return out


def count_via_gateway(gw: str) -> str:
    for cmd in (
        f"/routing route print count-only where gateway={gw}",
        f"/routing route print count-only where bgp and gateway={gw}",
        f"/routing route print count-only where protocol=bgp and gateway={gw}",
    ):
        r = ros(cmd)
        if r and "bad command" not in r and "expected" not in r:
            return r
    return "N/A"


def main() -> int:
    load_lab_env()
    print(f"=== RouterOS {H210} — 从其他 BGP 收到的路由 ===\n")
    print(ros("/routing bgp peer print detail without-paging"))
    print()

    peers = [
        ("139.159.30.17", "peer-as131320", "外网 AS131320（未 Established）"),
        ("10.133.153.200", "peer-lin200-153", "Linux 200 / AS63199（Established）"),
    ]
    for gw, name, desc in peers:
        n = count_via_gateway(gw)
        print(f"邻居 {name} ({gw}) — {desc}")
        print(f"  routing/route 经该 gateway 条数: {n}")
        print()

    print("--- BGP session ---")
    print(ros("/routing bgp session print detail without-paging")[:6000])
    print()
    print("--- 本机配置（向外通告，非「收到」）---")
    print(f"bgp network（本机重分发通告）: {ros('/routing bgp network print count-only')}")
    print(f"ip route 总数: {ros('/ip route print count-only')}")
    print(f"routing/route 中 protocol=bgp: {ros('/routing route print count-only where protocol=bgp')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
