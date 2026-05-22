#!/usr/bin/env python3
"""Diagnose ARP spoof on 109."""
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


def main() -> None:
    load_env()
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
    py = f"""
import sqlite3
from pathlib import Path
db = Path("{remote}/data.db")
conn = sqlite3.connect(db)
print("=== arp_spoof_settings ===")
for r in conn.execute("SELECT * FROM arp_spoof_settings"):
    print(r)
print("=== arp_spoof_targets (enabled) ===")
for r in conn.execute(
    "SELECT id, enabled, spoof_gateway_ip, satellite_vrf, egress_iface, policy_mode "
    "FROM arp_spoof_targets ORDER BY id"
):
    print(r)
conn.close()
"""
    script = f"""set -e
REMOTE={remote}
cd "$REMOTE"
echo '=== processes ==='
pgrep -af 'arp_spoof|uvicorn' || true
echo '=== scapy ==='
./venv/bin/python -c 'import scapy; print(scapy.__version__)' 2>/dev/null || python3 -c 'import scapy; print(scapy.__version__)' 2>/dev/null || echo NO_SCAPY
echo '=== arp log tail ==='
tail -40 /tmp/arp_spoof_daemon.log 2>/dev/null || echo no_log
echo '=== global via API ==='
curl -sf http://127.0.0.1:8808/api/global 2>/dev/null | python3 -c 'import sys,json; g=json.load(sys.stdin); print("arp_spoof_enabled=", g.get("arp_spoof_enabled"))' || echo api_fail
echo '=== arp targets API ==='
curl -sf http://127.0.0.1:8808/api/arp-spoof/targets 2>/dev/null | python3 -c '
import sys,json
t=json.load(sys.stdin)
print("targets=", len(t))
for x in t[:8]:
 print(x.get("id"), x.get("enabled"), x.get("spoof_gateway_ip"), x.get("egress_iface"))
' || echo targets_api_fail
echo '=== eno1np0 addrs (spoof) ==='
ip -br addr show eno1np0 2>/dev/null || true
ip -br addr show enp59s0f0np0 2>/dev/null | head -3
echo '=== iv249 if any ==='
ip -br link show type ipvlan 2>/dev/null | head -10 || ip link | grep -E 'iv249|@eno1' | head -10
echo '=== nft icmp accept spoof ==='
nft list ruleset 2>/dev/null | grep -E 'spoof|139.159.43.249|echo-request|mtr' | head -25 || true
echo '=== neigh 249 on eno1np0 ==='
ip neigh show 139.159.43.249 dev eno1np0 2>/dev/null || ip neigh show 139.159.43.249 2>/dev/null || true
python3 - <<'PY'
{py}
PY
"""
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
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=90)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace")
    if err.strip():
        print("stderr:", err)
    c.close()


if __name__ == "__main__":
    main()
