#!/usr/bin/env python3
"""只读：gobgp-rr/249 路由宣告是否已从下游(源249)聚合并发出。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit(2)

DEPLOY = Path(__file__).resolve().parent


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        if name == "env":
            break


def main() -> None:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        print("need MTR_OP_SSH_PASSWORD", file=sys.stderr)
        sys.exit(2)

    script = r"""
set +e
echo "=== 1) SQLite meta ==="
python3 <<'PY'
import sqlite3
c=sqlite3.connect('/root/mtr_op/data.db')
c.row_factory=sqlite3.Row
q="SELECT vrf,neighbor_ip,role,source_ip,advertise_routes,store_received_routes FROM bgp_neighbor_meta ORDER BY vrf,neighbor_ip"
for r in c.execute(q):
    print(dict(r))
print("--- aggregate peers (source_ip=RR_ADDR 249) ---")
for r in c.execute(
    "SELECT vrf,neighbor_ip FROM bgp_neighbor_meta WHERE source_ip=? AND neighbor_ip!=?",
    ('139.159.43.249','139.159.43.249')):
    print(dict(r))
PY

echo ""
echo "=== 2) OP RR row (full, non-fast) ==="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors/gobgp-rr/139.159.43.249" | python3 -m json.tool 2>/dev/null | head -30

echo ""
echo "=== 3) downstream peer rib count 208 ==="
curl -sf "http://127.0.0.1:9179/api/rib/routes/count?window=downstream&vrf=vbgp13915943249&neighbor_ip=139.159.43.208"
echo ""

echo ""
echo "=== 4) OP + Agent advertise/status ==="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors/gobgp-rr/139.159.43.249/advertise/status"
echo ""
curl -sf "http://127.0.0.1:9179/api/rib/advertise/status?task_id=gobgp-rr-139.159.43.249-advertise" 2>/dev/null || echo "agent_task_not_found"
echo ""

echo ""
echo "=== 5) Agent neighbors pfx ==="
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
d=json.load(sys.stdin)
for n in d.get('neighbors') or []:
    if n.get('address') in ('139.159.43.249','139.159.43.208'):
        print(n)
"

echo ""
echo "=== 6) journal rr-aggregate / 105.92 (48h) ==="
journalctl -u bgp-agent --since '48 hours ago' --no-pager 2>/dev/null | grep -iE 'rr-aggregate|249-advertise|105\.92|done rr aggregate|no downstream routes' | tail -25
"""

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ.get("MTR_OP_HOST", "101.89.68.109"),
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=pw,
        timeout=30,
    )
    i, o, e = c.exec_command("bash -se", timeout=90)
    i.write(script)
    i.channel.shutdown_write()
    print(o.read().decode(errors="replace") + e.read().decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
