#!/usr/bin/env python3
"""109：补全 2110/2111 与 105.94 邻居（与 transit 脚本一致，网关均不填）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

DIR = Path(__file__).resolve().parent
REMOTE = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
DOWN = os.environ.get("MTR_OP_DOWNSTREAM_IFACE", "eno1np0")
UP = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0")
PEER = os.environ.get("MTR_BGP_IPVLAN_PEER_IP", "139.159.43.208")
RR = os.environ.get("RR_ADDR", "139.159.43.249")
SRC = os.environ.get("ROUTER_ID", "139.159.43.207")
CLIENT = os.environ.get("MTR_DOWNSTREAM_CLIENT_NEIGH_HOSTS", "139.159.105.94").split(",")[0].strip()


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
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ["MTR_OP_HOST"].strip(),
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    script = f"""
set -e
DOWN={DOWN!r}
UP={UP!r}
PEER={PEER!r}
RR={RR!r}
SRC={SRC!r}
CLIENT={CLIENT!r}

# 2110 去程（43.0/24 走上联 dev，供 FIB via 206/249 解析；208/32 仍走下联）
ip route replace table 2110 "$PEER/32" dev "$DOWN" scope link
ip route replace table 2110 139.159.43.0/24 dev "$UP" scope link
ip route replace table 2110 default via "$RR" dev "$UP" src "$SRC"

# 2111 回程（无 via）
ip route replace table 2111 139.159.105.92/30 dev "$DOWN" scope link
ip route replace table 2111 "$PEER/32" dev "$DOWN" scope link

# rule（幂等）
ip -4 rule del pref 30 2>/dev/null || true
ip -4 rule add pref 30 iif "$DOWN" lookup 2110
while ip -4 rule del pref 29 to 139.159.105.92/30 2>/dev/null; do :; done
ip -4 rule add pref 29 to 139.159.105.92/30 lookup 2111

# 105.94 静态邻居
ping -c1 -W1 "$PEER" >/dev/null 2>&1 || true
MAC=$(ip neigh show dev "$DOWN" | awk -v p="$PEER" '$1==p {{print $3; exit}}')
if [ -n "$MAC" ] && [ "$MAC" != "FAILED" ]; then
  ip neigh replace "$CLIENT" lladdr "$MAC" dev "$DOWN" nud permanent
  echo "neigh $CLIENT -> $MAC"
else
  echo "WARN: no MAC for $PEER"
fi

echo "=== verify ==="
ip route get 8.8.8.8 from {CLIENT} iif "$DOWN" | head -1
ip route get {CLIENT} from 8.8.8.8 iif "$UP" | head -1

cd {REMOTE}
./venv/bin/python3 -c "
import sqlite3
c=sqlite3.connect('{REMOTE}/data.db')
c.execute('UPDATE static_routes SET enabled=1, gateway_ip=\"\" WHERE dst_cidr LIKE \\\"%105.92%\\\"')
c.commit()
print('db', list(c.execute('SELECT id,enabled,gateway_ip FROM static_routes WHERE dst_cidr LIKE \\\"%105.92%\\\"')))
"
curl -sf -X POST http://127.0.0.1:8808/api/static-routes/apply -H 'Content-Type: application/json' -d '{{}}' || true
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=90)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode()
    print(out, end="")
    c.close()


if __name__ == "__main__":
    main()
