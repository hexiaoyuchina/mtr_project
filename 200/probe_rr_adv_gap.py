#!/usr/bin/env python3
"""对比 RR 行：下游库条数 vs 聚合去重 vs pfx_adv / ROS。"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
RR = "10.133.153.204"


def load_env() -> str:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def http_json(url: str, timeout: int = 120):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def root(pw: str, script: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(os.environ.get("MTR_OP_HOST", "10.133.151.200"), username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=300)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> None:
    pw = load_env()
    op = f"http://{os.environ.get('MTR_OP_HOST', '10.133.151.200')}:8808"
    agent = "http://127.0.0.1:9179"

    print(
        root(
            pw,
            f"""
set -e
export OP='http://127.0.0.1:8808'
export AG='http://127.0.0.1:9179'
python3 <<'PY'
import json, os, sqlite3, urllib.parse, urllib.request

def get(u):
    with urllib.request.urlopen(u, timeout=120) as r:
        return json.load(r)

op = os.environ["OP"]
ag = os.environ["AG"]
db = "/root/mtr_op/data/mtr.db"
RR = "{RR}"

# OP neighbors
neighbors = get(op + "/api/bgp/neighbors")
rr = next((n for n in neighbors if n.get("neighbor_ip")==RR and n.get("vrf")=="gobgp-rr"), {{}})
print("=== OP gobgp-rr ===")
print(json.dumps({{k: rr.get(k) for k in (
    "session_state","routes_received","routes_sent","routes_cached",
    "advertise_routes","source_ip","store_received_routes")}}, indent=2))

# downstream with source_ip=RR
conn = sqlite3.connect(db)
rows = conn.execute(
    "SELECT vrf, neighbor_ip, role, advertise_routes FROM bgp_neighbor_meta WHERE source_ip=? AND neighbor_ip!=?",
    (RR, RR),
).fetchall()
print("\\n=== meta source_ip=%s downstream ===" % RR)
print("peers:", len(rows))
total_raw = 0
for vrf, nip, role, ar in rows:
    w = "downstream"
    q = urllib.parse.urlencode({{"window": w, "vrf": vrf, "neighbor_ip": nip}})
    cnt = int(get(ag + "/api/rib/routes/count?" + q).get("count") or 0)
    total_raw += cnt
    print(f"  {{vrf}} {{nip}} ar={{ar}} rib={{cnt}}")
print("sum_rib (may double-count prefixes):", total_raw)

# agent RR neighbor
for n in get(ag + "/api/neighbors").get("neighbors", []):
    if n.get("address") == RR:
        print("\\n=== Agent RR peer ===")
        print(json.dumps(n, indent=2))

# advertise task
tid = f"gobgp-rr-{{RR}}-advertise"
st = get(ag + "/api/rib/advertise/status?task_id=" + urllib.parse.quote(tid))
print("\\n=== rib job", tid, "===")
print(json.dumps(st, indent=2))

# gobgp global / rr adj
import subprocess
for cmd in [
    "gobgp -p 50052 neighbor 10.133.153.204 2>/dev/null | head -5",
    "gobgp -p 50052 neighbor 10.133.153.204 adj-out 2>/dev/null | wc -l",
]:
    try:
        o = subprocess.check_output(cmd, shell=True, text=True, timeout=60)
        print(cmd, "->", o.strip()[:200])
    except Exception as e:
        print(cmd, "ERR", e)
PY
""",
        )
    )


if __name__ == "__main__":
    main()
