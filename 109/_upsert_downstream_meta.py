#!/usr/bin/env python3
"""Ensure bgp_neighbor_meta has downstream 208 for each satellite VRF on 109."""
from __future__ import annotations

import os
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
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()
        if name == "env":
            break


REMOTE_PY = """
import sqlite3
from datetime import datetime, timezone

db = "/root/mtr_op/data.db"
conn = sqlite3.connect(db)
now = datetime.now(timezone.utc).isoformat()
rows = [
    ("vbgp13915943247", "139.159.43.208", "downstream", "", "139.159.43.247"),
    ("vbgp13915943249", "139.159.43.208", "downstream", "", "139.159.43.249"),
]
for vrf, nip, role, note, sip in rows:
    conn.execute(
        '''INSERT OR REPLACE INTO bgp_neighbor_meta
           (vrf, neighbor_ip, role, note, source_ip, advertise_routes, advertise_routes_from, created_at)
           VALUES (?,?,?,?,?,0,'',?)''',
        (vrf, nip, role, note, sip, now),
    )
    print("upsert", vrf, nip, sip)
conn.commit()
for r in conn.execute(
    "SELECT vrf, neighbor_ip, source_ip, role FROM bgp_neighbor_meta WHERE vrf LIKE 'vbgp%' ORDER BY vrf"
):
    print(r)
conn.close()
"""


def main() -> None:
    load_env()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ["MTR_OP_HOST"],
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    _, stdout, stderr = c.exec_command(
        f"python3 <<'PY'\n{REMOTE_PY}\nPY", timeout=60
    )
    print(stdout.read().decode())
    err = stderr.read().decode()
    if err.strip():
        print("STDERR:", err)
    c.close()


if __name__ == "__main__":
    main()
