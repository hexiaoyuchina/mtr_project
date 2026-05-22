#!/usr/bin/env python3
"""查 105.94/32 是否由 ARP 引流维护。"""
from __future__ import annotations

import os
from pathlib import Path

import paramiko

DIR = Path(__file__).resolve().parent


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
        break


def main() -> None:
    load_env()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ["MTR_OP_HOST"].strip(),
        username="root",
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    script = """
echo "=== eno1np0 addrs ==="
ip -4 addr show dev eno1np0 | grep inet || true
echo "=== .arp_daemon_assigned_host.json ==="
cat /root/mtr_op/.arp_daemon_assigned_host.json 2>/dev/null || echo "(none)"
/root/mtr_op/venv/bin/python3 <<'PY'
import sqlite3
c = sqlite3.connect("/root/mtr_op/data.db")
print("arp_on", c.execute("SELECT arp_spoof_enabled FROM arp_spoof_settings").fetchone())
rows = list(c.execute(
    "SELECT id,enabled,spoof_gateway_ip,egress_iface,satellite_vrf "
    "FROM arp_spoof_targets WHERE spoof_gateway_ip='139.159.105.94' OR egress_iface='eno1np0'"
))
print("targets", len(rows))
for r in rows:
    print(r)
PY
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=20)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
