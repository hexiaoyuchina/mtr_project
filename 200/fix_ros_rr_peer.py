#!/usr/bin/env python3
"""启用 ROS peer-lin200-153 并验收 200 RX RR 会话。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H200, H210 = "10.133.151.200", "10.133.151.210"
RR, LOCAL = "10.133.153.204", "10.133.153.200"


def load_env() -> str:
    pw = "1234qwer"
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    return os.environ.get("MTR_OP_SSH_PASSWORD", pw)


def ros(cmd: str, pw: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H210, username="admin", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command(cmd if cmd.startswith("/") else f"/{cmd}", timeout=60)
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def root(script: str, pw: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H200, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=120)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    pw = load_env()
    print("=== ROS：修复 peer-lin200-153 ===\n")
    for cmd in [
        '/routing filter set [find name=lin200-153-in] disabled=no',
        '/routing bgp peer set [find name=peer-lin200-153] disabled=no in-filter=""',
        '/routing bgp peer print detail where name=peer-lin200-153',
    ]:
        print(f">>> {cmd}\n{ros(cmd, pw)}\n")

    print("等待 BGP 建连 (25s)…")
    time.sleep(25)
    print(ros("/routing bgp peer print detail where name=peer-lin200-153", pw))

    print("\n=== 200：网络 + RR ===\n")
    print(
        root(
            f"""
bash /root/mtr_op/remote-network-prereq.sh 2>/dev/null || true
curl -sf -X POST http://127.0.0.1:9179/api/rr/unfreeze >/dev/null
curl -sf -X POST http://127.0.0.1:8808/api/bgp/restore-agent -H 'Content-Type: application/json' -d '{{}}' | head -c 400
echo
sleep 5
ss -tnp state established | grep -E '{LOCAL}|{RR}' | grep ':179' || echo 'no_estab_179'
curl -s http://127.0.0.1:9179/api/rr/status | head -c 500
echo
""",
            pw,
        )
    )

    try:
        with urllib.request.urlopen(f"http://{H200}:8808/api/bgp/neighbors", timeout=30) as r:
            for row in json.loads(r.read().decode()):
                if row.get("vrf") == "gobgp-rr":
                    print("\nOP gobgp-rr:", json.dumps(row, ensure_ascii=False))
                    return 0 if str(row.get("session_state")).lower() == "established" else 1
    except Exception as ex:
        print("OP check failed:", ex)
    return 1


if __name__ == "__main__":
    sys.exit(main())
