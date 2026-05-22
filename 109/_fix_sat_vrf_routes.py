#!/usr/bin/env python3
"""Fix missing VRF routes for multi satellite VRF (247/249) on 109."""
from __future__ import annotations

import os
from pathlib import Path

import paramiko

DEPLOY_DIR = Path(__file__).resolve().parent
PEER = "139.159.43.208"
SPOOFS = ("245", "247", "249")


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


REMOTE_PY = r"""
import sqlite3
from pathlib import Path

db = Path("/root/mtr_op/data.db")
conn = sqlite3.connect(db)
print("=== bgp_neighbor_meta ===")
for r in conn.execute(
    "SELECT vrf, neighbor_ip, source_ip, role FROM bgp_neighbor_meta ORDER BY vrf"
):
    print(r)
conn.close()

PEER = "139.159.43.208"
for last in ("245", "247", "249"):
    ip = f"139.159.43.{last}"
    vrf = f"vbgp13915943{last}"
    iv = f"iv{last}"
    import subprocess
    for cmd in (
        ["ip", "route", "replace", "vrf", vrf, f"{PEER}/32", "dev", iv, "src", ip],
        ["ip", "route", "replace", "vrf", vrf, "139.159.43.0/24", "dev", iv, "src", ip],
    ):
        p = subprocess.run(cmd, capture_output=True, text=True)
        print(" ".join(cmd), "rc=", p.returncode, (p.stderr or p.stdout or "").strip()[:80])
    # static neigh 208 if missing (same MAC as 245)
    p = subprocess.run(["ip", "neigh", "show", PEER, "dev", "iv245"], capture_output=True, text=True)
    mac = ""
    if p.returncode == 0 and "lladdr" in p.stdout:
        mac = p.stdout.split("lladdr", 1)[1].split()[0]
    if mac:
        subprocess.run(
            ["ip", "neigh", "replace", PEER, "lladdr", mac, "dev", iv, "nud", "permanent"],
            capture_output=True,
        )
        print(f"neigh {PEER} dev {iv} -> {mac}")
    p2 = subprocess.run(
        ["ip", "vrf", "exec", vrf, "ping", "-c1", "-W2", "-I", iv, PEER],
        capture_output=True,
        text=True,
    )
    print(f"ping from {ip}:", (p2.stdout or p2.stderr or "").strip().split("\n")[-1])
    p3 = subprocess.run(
        ["ip", "route", "get", PEER, "from", ip],
        capture_output=True,
        text=True,
    )
    print(f"route get from {ip}:", (p3.stdout or "").strip())
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
    _, stdout, stderr = c.exec_command(f"python3 <<'PY'\n{REMOTE_PY}\nPY", timeout=60)
    print(stdout.read().decode("utf-8", errors="replace"))
    err = stderr.read().decode("utf-8", errors="replace")
    if err.strip():
        print("STDERR:", err)
    c.close()


if __name__ == "__main__":
    main()
