#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
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
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    script = r"""
echo "=== DB static 105.92 ==="
/root/mtr_op/venv/bin/python3 <<'PY'
import sqlite3
c = sqlite3.connect("/root/mtr_op/data.db")
for r in c.execute("SELECT id,enabled,dst_cidr,gateway_ip,egress_iface,table_id FROM static_routes WHERE dst_cidr LIKE '%105.92%'"):
    print(r)
print("hijack", c.execute("SELECT hijack_enabled FROM global_config").fetchone())
print("hop_enabled", c.execute("SELECT COUNT(*) FROM hop_replace_rules WHERE enabled=1").fetchone())
PY

echo "=== kernel 2111 + rules ==="
ip route show table 2111
ip -4 rule list | grep -E '^29:|^30:'

echo "=== path tests ==="
ip route get 8.8.8.8 from 139.159.105.94 iif eno1np0 | head -1
ip route get 139.159.105.94 from 8.8.8.8 iif enp59s0f0np0 | head -1

echo "=== neigh ==="
ip neigh show dev eno1np0 | grep -E '105\.94|43\.208'

echo "=== te map ==="
cat /tmp/mtr_te_map.env 2>/dev/null || echo "(no te map file)"
tail -2 /tmp/te_rewrite_nfqueue.log 2>/dev/null

echo "=== ping 105.94 ==="
ping -c1 -W1 -I eno1np0 139.159.105.94 2>&1 | tail -2

echo "=== curl static-routes API ==="
curl -sS http://127.0.0.1:8808/api/static-routes/10 2>/dev/null || curl -sS 'http://127.0.0.1:8808/api/static-routes?reconcile=1' | /root/mtr_op/venv/bin/python3 -c "import sys,json; rows=json.load(sys.stdin); print([x for x in rows if '105.92' in x.get('dst_cidr','')])"
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=60)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
