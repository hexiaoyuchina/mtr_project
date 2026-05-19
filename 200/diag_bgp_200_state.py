#!/usr/bin/env python3
"""诊断 Linux 200：data.db、OP 邻居、Agent 会话。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H200 = "10.133.151.200"


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def pw() -> str:
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def root(script: str, timeout: int = 120) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H200, username="root", password=pw(), timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    load_lab_env()
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")

    print(f"=== Linux {H200} BGP 状态诊断 ===\n")
    print(
        root(
            f"""
export REMOTE={remote}
echo '--- data.db ---'
ls -la $REMOTE/data.db $REMOTE/data.db-* 2>/dev/null || echo 'NO data.db'
if [ -f $REMOTE/data.db ]; then
  $REMOTE/venv/bin/python3 - <<'PY'
import sqlite3, os
db = os.environ.get("REMOTE", "/root/mtr_op") + "/data.db"
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
try:
    n = conn.execute("SELECT COUNT(*) FROM bgp_neighbor_meta").fetchone()[0]
    print("bgp_neighbor_meta rows:", n)
    for row in conn.execute(
        "SELECT vrf, neighbor_ip, role, source_ip, advertise_routes, store_received_routes FROM bgp_neighbor_meta ORDER BY vrf, neighbor_ip"
    ):
        print(dict(row))
except Exception as e:
    print("meta query error:", e)
conn.close()
PY
fi
echo '--- services ---'
systemctl is-active bgp-agent 2>/dev/null || echo bgp-agent-inactive
pgrep -af 'uvicorn app.main' || echo 'no uvicorn'
curl -sf http://127.0.0.1:8808/health && echo ' op_ok' || echo ' op_FAIL'
curl -sf http://127.0.0.1:9179/health && echo ' agent_ok' || echo ' agent_FAIL'
echo '--- OP neighbors (curl) ---'
curl -sf http://127.0.0.1:8808/api/bgp/neighbors 2>/dev/null | python3 -m json.tool 2>/dev/null | head -80 || echo 'neighbors API fail'
echo '--- agent status ---'
curl -sf http://127.0.0.1:9179/api/status 2>/dev/null | python3 -m json.tool 2>/dev/null | head -40 || true
echo '--- freeze ---'
curl -sf http://127.0.0.1:9179/api/peers/freeze-status 2>/dev/null | python3 -m json.tool 2>/dev/null | head -50 || true
echo '--- :179 ---'
ss -tn state established '( sport = :179 or dport = :179 )' 2>/dev/null | head -15 || true
echo '--- recent op log ---'
tail -30 /tmp/mtr_op.log 2>/dev/null || true
echo '--- recent agent log ---'
journalctl -u bgp-agent -n 15 --no-pager 2>/dev/null || true
"""
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
