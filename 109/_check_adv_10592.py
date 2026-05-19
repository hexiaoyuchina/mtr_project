#!/usr/bin/env python3
"""只读：139.159.105.92/30 是否已向上游 RR 宣告。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit(2)

DEPLOY = Path(__file__).resolve().parent
PREFIX = "139.159.105.92/30"
VRF = "vbgp13915943249"
PEER = "139.159.43.208"
RR = "139.159.43.249"
RR_VRF = "gobgp-rr"


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


SCRIPT = r"""
set +e
PY=python3
PREFIX=__PREFIX__
VRF=__VRF__
PEER=__PEER__
RR=__RR__
RR_VRF=__RR_VRF__

echo "=== meta (208 / 249) ==="
python3 <<'PY'
import sqlite3
c=sqlite3.connect('/root/mtr_op/data.db')
c.row_factory=sqlite3.Row
for r in c.execute(
    "SELECT vrf,neighbor_ip,role,source_ip,advertise_routes,store_received_routes FROM bgp_neighbor_meta WHERE vrf=? OR neighbor_ip IN (?,?)",
    ('vbgp13915943249','139.159.43.208','139.159.43.249')):
    print(dict(r))
PY

echo ""
echo "=== downstream peer rib count (208) ==="
curl -sf "http://127.0.0.1:9179/api/rib/routes/count?window=downstream&vrf=$VRF&neighbor_ip=$PEER" || echo count_fail

echo ""
echo "=== RR advertise/status (OP) ==="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors/$RR_VRF/$RR/advertise/status"

echo ""
echo ""
echo "=== Agent rib advertise/status (RR task) ==="
curl -sf "http://127.0.0.1:9179/api/rib/advertise/status?task_id=$RR_VRF-$RR-advertise" || echo agent_task_gone

echo ""
echo ""
echo "=== RX global/local: prefix present? ==="
curl -sf "http://127.0.0.1:9179/api/routes?page_size=50000" -o /tmp/rx_all.json 2>/dev/null
$PY <<'PY'
import json
pfx = "__PREFIX__"
try:
    with open("/tmp/rx_all.json") as f:
        d = json.load(f)
except Exception as e:
    print("rx_routes_err", e)
    raise SystemExit(0)
items = d.get("routes") or []
hits = [r for r in items if str(r.get("prefix") or "") == pfx]
print("rx_effective_total", len(items))
print("prefix_in_rx_effective", bool(hits))
for h in hits[:2]:
    print(json.dumps(h, ensure_ascii=False))
PY

echo ""
echo "=== journal: 105.92 / rr-aggregate (72h) ==="
journalctl -u bgp-agent --since '72 hours ago' --no-pager 2>/dev/null | grep -iE '105\.92|rr-aggregate|249-advertise|done rr aggregate' | tail -20 || true
journalctl -u mtr-op --since '72 hours ago' --no-pager 2>/dev/null | grep -iE '105\.92|RR aggregate|249-advertise' | tail -10 || true
"""


def main() -> None:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        sys.exit(2)
    script = (
        SCRIPT.replace("__PREFIX__", PREFIX)
        .replace("__VRF__", VRF)
        .replace("__PEER__", PEER)
        .replace("__RR__", RR)
        .replace("__RR_VRF__", RR_VRF)
    )
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ.get("MTR_OP_HOST", "101.89.68.109"),
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=pw,
        timeout=30,
    )
    i, o, e = c.exec_command("bash -se", timeout=120)
    i.write(script)
    i.channel.shutdown_write()
    print(o.read().decode(errors="replace") + e.read().decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
