#!/usr/bin/env python3
"""诊断 ROS peer-as131320 -> 139.159.30.17 未 Established。"""
from __future__ import annotations

import os
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H210 = "10.133.151.210"
PEER = "139.159.30.17"


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def pw() -> str:
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def ros(cmd: str, timeout: int = 90) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H210, username="admin", password=pw(), timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command(cmd if cmd.startswith("/") else f"/{cmd}", timeout=timeout)
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    load_lab_env()
    cmds = [
        ("BGP peer", "/routing bgp peer print detail without-paging"),
        ("BGP instance", "/routing bgp instance print detail without-paging"),
        ("IP addresses", "/ip address print without-paging"),
        ("Route to peer", f"/ip route print detail where dst-address={PEER}"),
        ("Ping peer", f"/ping {PEER} count=4"),
        ("Log BGP", '/log print without-paging where topics~"bgp"'),
        ("Log peer IP", f'/log print without-paging where message~"{PEER}"'),
        ("Firewall filter stats", "/ip firewall filter print stats without-paging"),
        ("NAT", "/ip firewall nat print without-paging"),
        ("Connections :179", "/ip firewall connection print where (dst-port=179 or src-port=179)"),
    ]
    for title, cmd in cmds:
        print(f"=== {title} ===")
        try:
            print(ros(cmd)[:8000])
        except Exception as ex:
            print(f"ERROR: {ex}")
        print()

    print("=== Recent BGP log lines for 30.17 (last 40) ===")
    log = ros('/log print without-paging')
    hits = [ln for ln in log.splitlines() if PEER in ln or "131320" in ln]
    print("\n".join(hits[-40:]))
    print()
    print("=== BGP errors in log (last 25) ===")
    errs = [ln for ln in log.splitlines() if "bgp,error" in ln]
    print("\n".join(errs[-25:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
