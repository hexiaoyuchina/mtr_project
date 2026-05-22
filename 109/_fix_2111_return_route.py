#!/usr/bin/env python3
"""修正 109 table 2111 回程：105.92/30 勿 via 208，改为 scope link。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

DIR = Path(__file__).resolve().parent
ROOT = DIR.parent
REMOTE = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")


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
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not pw:
        raise SystemExit("MTR_OP_SSH_PASSWORD required")
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    user = os.environ.get("MTR_OP_SSH_USER", "root").strip()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        host,
        username=user,
        password=pw,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    script = f"""
set -e
echo "=== table 2111 before ==="
ip route show table 2111
echo "=== route get 105.94 return before ==="
ip route get 139.159.105.94 from 8.8.8.8 iif enp59s0f0np0 2>&1 | head -2 || true
echo "=== fix 105.92/30 scope link ==="
ip route replace table 2111 139.159.105.92/30 dev eno1np0 scope link
echo "=== after ==="
ip route show table 2111
ip route get 139.159.105.94 from 8.8.8.8 iif enp59s0f0np0 2>&1 | head -2 || true
cd {REMOTE}
export MTR_OP_DB={REMOTE}/data.db
if [ -x ./venv/bin/python3 ]; then PY=./venv/bin/python3; else PY=python3; fi
$PY - <<'PY'
import os, sqlite3
db = os.environ["MTR_OP_DB"]
conn = sqlite3.connect(db)
rows = conn.execute(
    "SELECT id, gateway_ip, enabled FROM static_routes WHERE dst_cidr LIKE '%105.92%'"
).fetchall()
print("db_rows", rows)
for rid, gw, _en in rows:
    if (gw or "").strip():
        conn.execute(
            'UPDATE static_routes SET gateway_ip="", updated_at=strftime("%Y-%m-%dT%H:%M:%SZ","now") WHERE id=?',
            (rid,),
        )
        print("cleared_gateway", rid)
conn.commit()
PY
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=60)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    print(out, end="")
    c.close()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
