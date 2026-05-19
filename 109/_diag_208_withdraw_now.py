#!/usr/bin/env python3
"""208：撤销是否完成、BGP、Winbox — 只读。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

DEPLOY_DIR = Path(__file__).resolve().parent


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY_DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        if name == "env":
            break


def main() -> int:
    load_env()
    pw = os.environ["MTR_OP_SSH_PASSWORD"]
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    script = r"""
set +e
echo '=== OP advertise status ==='
curl -sf http://127.0.0.1:8808/api/bgp/neighbors/vbgp13915943249/139.159.43.208/advertise/status
echo
echo '=== Agent rib job (same task_id) ==='
curl -sf 'http://127.0.0.1:9179/api/rib/advertise/status?task_id=vbgp13915943249-139.159.43.208-advertise'
echo
echo '=== journal withdraw/advertise last 2h ==='
journalctl -u bgp-agent --since '2 hours ago' --no-pager 2>/dev/null | \
  grep '139.159.43.208-advertise\|208-advertise' | tail -20
echo '=== 8291/22/icmp to 208 (vrf) ==='
ip vrf exec vbgp13915943249 ping -c2 -W2 139.159.43.208 | tail -3
timeout 2 bash -c 'echo >/dev/tcp/139.159.43.208/8291' 2>/dev/null && echo tcp8291_ok || echo tcp8291_fail
timeout 2 bash -c 'echo >/dev/tcp/139.159.43.208/22' 2>/dev/null && echo tcp22_ok || echo tcp22_fail
"""
    i, o, e = c.exec_command("bash -se", timeout=60)
    i.write(script)
    i.channel.shutdown_write()
    print(o.read().decode(errors="replace"))
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
