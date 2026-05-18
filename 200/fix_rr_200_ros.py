#!/usr/bin/env python3
"""200 上恢复 RR RX + 查 201/210。"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
PW = "1234qwer"
H200, H201, H210 = "10.133.151.200", "10.133.151.201", "10.133.151.210"


def pw() -> str:
    if (LAB / "lab.env").is_file():
        for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()
    return os.environ.get("MTR_OP_SSH_PASSWORD", PW)


def root(host: str, script: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw(), timeout=30, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=120)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")
    c.close()
    return out


def ros(cmd: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H210, username="admin", password=pw(), timeout=30, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command(cmd if cmd.startswith("/") else f"/{cmd}", timeout=60)
    out = o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    print("=== RouterOS 210 BGP ===")
    print(ros("/routing bgp connection print detail"))
    print(ros("/routing bgp session print detail"))

    script = textwrap.dedent(
        r"""
        set -e
        PR=/root/mtr_op/remote-network-prereq.sh
        [ -f "$PR" ] && bash "$PR" || true
        echo '--- ping RR ---'
        ping -c2 -W2 -I ens224 10.133.153.204 || true
        echo '--- remove stray TX ---'
        curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove \
          -H 'Content-Type: application/json' \
          -d '{"vrf":"gobgp-rr","address":"10.133.153.204"}' || true
        echo '--- rr/config ---'
        curl -sf -X POST http://127.0.0.1:9179/api/rr/config \
          -H 'Content-Type: application/json' \
          -d '{"address":"10.133.153.204","remote_as":63199,"local_address":"10.133.153.200"}'
        echo
        sleep 5
        echo '--- status ---'
        curl -s http://127.0.0.1:9179/api/rr/status | python3 -m json.tool
        curl -s http://127.0.0.1:9179/api/neighbors | python3 <<'PY'
import json,sys
for n in json.load(sys.stdin).get("neighbors",[]):
    if n.get("address")=="10.133.153.204":
        print("neighbor", n)
PY
        ss -tnp | grep -E '153\.200.*179|153\.204.*179' | head -10 || true
        journalctl -u bgp-agent -n 30 --no-pager | grep -iE '153.204|Peer Up|estab' | tail -15 || true
        echo '--- OP POST neighbor ---'
        curl -sf -X POST http://127.0.0.1:8808/api/bgp/neighbors \
          -H 'Content-Type: application/json' \
          -d '{"vrf":"gobgp-rr","neighbor_ip":"10.133.153.204","remote_as":63199,"local_as":63199,"source_ip":"10.133.153.200","role":"rr"}' && echo ok || echo skip
        curl -s http://127.0.0.1:8808/api/bgp/neighbors | python3 <<'PY'
import json,sys
for r in json.load(sys.stdin):
    if r.get("neighbor_ip")=="10.133.153.204":
        print(json.dumps(r, ensure_ascii=False))
PY
        """
    )
    print("=== Linux 200 ===")
    print(root(H200, script))

    print("=== Linux 201 FRR 153.204 (not RR uplink) ===")
    print(
        root(
            H201,
            "vtysh -c 'show bgp neighbors 10.133.153.204' 2>/dev/null | head -40\n"
            "vtysh -c 'show bgp neighbors 10.133.152.200' 2>/dev/null | head -20\n",
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
