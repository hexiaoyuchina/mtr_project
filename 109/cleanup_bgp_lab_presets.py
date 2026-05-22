#!/usr/bin/env python3
"""从 109（或 env 指定主机）SQLite 删除已废弃的实验室 BGP 预设行。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import paramiko

DEPLOY_DIR = Path(__file__).resolve().parent
ROOT = DEPLOY_DIR.parent
REMOTE_DB = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").rstrip("/") + "/data.db"

# 与已删除的 parse_bgp_db_presets 内置默认一致
PRESET_ROWS = (
    ("gobgp-tx", "139.159.43.208"),
    ("vrf2103", "10.133.153.204"),
    ("default", "10.133.152.204"),
    ("vrf2102", "10.133.152.204"),
    ("default", "139.159.43.249"),
)


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


def cleanup_local(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    n = 0
    for vrf, ip in PRESET_ROWS:
        cur = conn.execute(
            "DELETE FROM bgp_neighbor_meta WHERE vrf = ? AND neighbor_ip = ?",
            (vrf, ip),
        )
        n += cur.rowcount
    cur = conn.execute(
        "DELETE FROM bgp_neighbor_meta WHERE neighbor_ip LIKE '10.133.%'"
    )
    n += cur.rowcount
    conn.commit()
    left = list(
        conn.execute(
            "SELECT vrf, neighbor_ip, role, note FROM bgp_neighbor_meta ORDER BY vrf, neighbor_ip"
        )
    )
    conn.close()
    print(f"deleted_preset_rows={n}")
    print("remaining bgp_neighbor_meta:")
    for r in left:
        print("\t".join(str(x) for x in r))
    return n


def cleanup_remote() -> None:
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not pw:
        raise SystemExit("请设置 MTR_OP_SSH_PASSWORD（109/env）")
    script = f"""
python3 - <<'PY'
import sqlite3
db = "{REMOTE_DB}"
preset = {list(PRESET_ROWS)!r}
conn = sqlite3.connect(db)
n = 0
for vrf, ip in preset:
    cur = conn.execute(
        "DELETE FROM bgp_neighbor_meta WHERE vrf = ? AND neighbor_ip = ?",
        (vrf, ip),
    )
    n += cur.rowcount
cur = conn.execute("DELETE FROM bgp_neighbor_meta WHERE neighbor_ip LIKE '10.133.%'")
n += cur.rowcount
conn.commit()
print("deleted_rows", n)
for r in conn.execute(
    "SELECT vrf, neighbor_ip, role, note FROM bgp_neighbor_meta ORDER BY vrf, neighbor_ip"
):
    print("\\t".join(str(x) for x in r))
conn.close()
PY
"""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ.get("MTR_OP_HOST", "101.89.68.109"),
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=pw,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    i, o, e = c.exec_command("bash -se", timeout=60)
    i.write(script)
    i.channel.shutdown_write()
    print(o.read().decode(errors="replace"))
    err = e.read().decode(errors="replace")
    if err.strip():
        print("stderr:", err)
    code = o.channel.recv_exit_status()
    c.close()
    if code != 0:
        raise SystemExit(code)


def main() -> None:
    load_env()
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--local":
        db = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "service" / "data.db"
        cleanup_local(db)
        return
    cleanup_remote()
    print("cleanup_bgp_lab_presets_ok")


if __name__ == "__main__":
    main()
