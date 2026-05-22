#!/usr/bin/env python3
"""Dump BGP-related rows from 109 OP SQLite."""
from __future__ import annotations

import os
import textwrap
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


REMOTE_PY = textwrap.dedent(
    """
    import sqlite3
    from pathlib import Path

    db = Path("/root/mtr_op/data.db")
    print("database=", db)
    print("size_bytes=", db.stat().st_size)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    def dump(title, sql):
        print()
        print("===", title, "===")
        rows = conn.execute(sql).fetchall()
        if not rows:
            print("(empty)")
            return
        cols = rows[0].keys()
        print("\\t".join(cols))
        for r in rows:
            print("\\t".join("" if r[c] is None else str(r[c]) for c in cols))

    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'bgp%' ORDER BY name"
        )
    ]
    print()
    print("=== BGP tables (row counts) ===")
    for t in tables:
        n = conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
        print(t + ":", n)

    dump(
        "bgp_neighbor_meta (full)",
        "SELECT vrf, neighbor_ip, role, source_ip, advertise_routes, advertise_routes_from, "
        "store_received_routes, note, created_at FROM bgp_neighbor_meta ORDER BY vrf, neighbor_ip",
    )
    dump("bgp_rib_sync_state", "SELECT * FROM bgp_rib_sync_state")
    dump("bgp_peer_snapshot", "SELECT * FROM bgp_peer_snapshot ORDER BY vrf, neighbor_ip")
    dump("bgp_sticky_frr", "SELECT * FROM bgp_sticky_frr")
    dump(
        "bgp_learned_routes (group top 30)",
        "SELECT vrf, neighbor_ip, role, route_window, COUNT(*) AS cnt FROM bgp_learned_routes "
        "GROUP BY vrf, neighbor_ip, role, route_window ORDER BY cnt DESC LIMIT 30",
    )
    dump(
        "bgp_upstream_route_cache (by learn_vrf)",
        "SELECT learn_vrf, COUNT(*) AS cnt FROM bgp_upstream_route_cache "
        "GROUP BY learn_vrf ORDER BY cnt DESC LIMIT 15",
    )
    for t in ("satellite_vrf", "arp_spoof_targets", "arp_spoof_settings"):
        try:
            n = conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
            print()
            print("=== " + t + " (count=" + str(n) + ") ===")
            if n:
                dump(t, "SELECT * FROM " + t + " LIMIT 20")
        except sqlite3.OperationalError as e:
            print()
            print("=== " + t + ": " + str(e) + " ===")
    conn.close()
    """
)


def main() -> None:
    load_env()
    remote_dir = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
    py = REMOTE_PY.replace("/root/mtr_op/data.db", f"{remote_dir}/data.db")
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
    stdin, stdout, stderr = c.exec_command("python3 -", timeout=300)
    stdin.write(py)
    stdin.channel.shutdown_write()
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace")
    if err.strip():
        print("stderr:", err)
    c.close()


if __name__ == "__main__":
    main()
