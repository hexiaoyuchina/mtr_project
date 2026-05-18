#!/usr/bin/env python3
"""排查 vbgp10133153204 / 152.204 启停后无法建连。"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
HOST_200 = "10.133.151.200"
HOST_201 = "10.133.151.201"
PW = "1234qwer"
VRF = "vbgp10133153204"
SPOOF = "10.133.153.204"
PEER = "10.133.152.204"


def ssh_run(host: str, script: str, password: str, timeout: int = 90) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=password, timeout=30, allow_agent=False, look_for_keys=False)
    try:
        _, stdout, stderr = c.exec_command(script, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        return (out + ("\n" + err if err.strip() else "")).strip()
    finally:
        c.close()


def main() -> int:
    env = LAB / "lab.env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()
    password = os.environ.get("MTR_OP_SSH_PASSWORD", PW)

    print("=" * 60)
    print(f"Linux 200 ({HOST_200})")
    print("=" * 60)
    script200 = textwrap.dedent(
        f"""
        set -e
        echo '--- OP neighbor ---'
        curl -s 'http://127.0.0.1:8808/api/bgp/neighbors' | python3 -c "
        import json,sys
        rows=json.load(sys.stdin)
        for r in rows:
          if r.get('vrf')=='{VRF}' and r.get('neighbor_ip')=='{PEER}':
            print(json.dumps(r, indent=2, ensure_ascii=False))
        "
        echo '--- Agent neighbors (grep) ---'
        curl -s 'http://127.0.0.1:9179/api/neighbors' | python3 -c "
        import json,sys
        d=json.load(sys.stdin)
        for n in d.get('neighbors',[]):
          if n.get('vrf')=='{VRF}' or n.get('address')=='{PEER}':
            print(n)
        "
        echo '--- freeze-status ---'
        curl -s 'http://127.0.0.1:9179/api/peers/freeze-status' | python3 -m json.tool 2>/dev/null | head -80
        echo '--- VRF / iface ---'
        ip link show | grep -E '{VRF}|iv153|153.204' || true
        ip -br addr show vrf {VRF} 2>/dev/null || ip vrf show | grep 153 || true
        ip route show vrf {VRF} 2>/dev/null | head -15 || true
        echo '--- ping peer from vrf ---'
        ip vrf exec {VRF} ping -c2 -W2 -I {SPOOF} {PEER} 2>&1 || true
        echo '--- BGP sockets ---'
        ss -tnp | grep -E '179|1836|bgp_agent' | head -30 || true
        echo '--- bgp-agent log ---'
        journalctl -u bgp-agent -n 40 --no-pager 2>/dev/null | tail -40
        """
    )
    print(ssh_run(HOST_200, script200, password))

    print("\n" + "=" * 60)
    print(f"Linux 201 ({HOST_201})")
    print("=" * 60)
    script201 = textwrap.dedent(
        f"""
        echo '--- ping 200 spoof ---'
        ping -c2 -W2 {SPOOF} 2>&1 || true
        echo '--- FRR bgp summary (if any) ---'
        vtysh -c 'show ip bgp summary' 2>/dev/null || true
        vtysh -c 'show bgp ipv4 unicast summary' 2>/dev/null || true
        echo '--- ss BGP ---'
        ss -tnp | grep -E ':179|bgpd|frr' | head -25 || true
        echo '--- neighbor config grep ---'
        vtysh -c 'show run' 2>/dev/null | grep -E '153.204|152.204|63199|neighbor' | head -40 || true
        """
    )
    try:
        print(ssh_run(HOST_201, script201, password))
    except Exception as e:
        print(f"201 SSH failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
