#!/usr/bin/env python3
import os
import sys
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H210, H200 = "10.133.151.210", "10.133.151.200"


def pw() -> str:
    p = "1234qwer"
    if (LAB / "lab.env").is_file():
        for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()
    return os.environ.get("MTR_OP_SSH_PASSWORD", p)


def ros(cmd: str) -> None:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H210, username="admin", password=pw(), timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command(cmd if cmd.startswith("/") else f"/{cmd}", timeout=60)
    print(o.read().decode() + e.read().decode())
    c.close()


def root(script: str) -> None:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H200, username="root", password=pw(), timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=90)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    print(o.read().decode() + e.read().decode())
    c.close()


def main() -> None:
    print("=== ROS peer / session ===")
    ros("/routing bgp peer print detail")
    ros("/routing bgp peer print stats")
    print("=== 200 vrf2103 ping / arp ===")
    root(
        """
        ip neigh show dev ens224 | grep 153.204 || true
        ip neigh show dev ens256 | grep 153.204 || true
        ip vrf exec vrf2103 ping -c2 -W2 10.133.153.204 || true
        traceroute -n -w1 -q1 -m5 10.133.153.204 2>&1 | head -8 || true
        curl -s http://127.0.0.1:9179/api/rr/status
        echo
        ss -tnp | grep '10.133.153.200.*10.133.153.204\\|10.133.153.204.*10.133.153.200' || echo 'no RR TCP'
        """
    )


if __name__ == "__main__":
    main()
