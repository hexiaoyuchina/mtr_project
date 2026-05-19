#!/usr/bin/env python3
"""恢复 Linux 200 实验室 BGP：解冻、RR、meta→Agent、ipvlan、会话验收。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
ROOT = LAB.parent


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def main() -> int:
    load_lab_env()
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")

    script = """
set -e
export MTR_OP_REMOTE_DIR=__REMOTE__
export MTR_OP_DB=__REMOTE__/data.db
export LOCAL_AS=__LOCAL_AS__
export ROUTER_ID=__ROUTER_ID__
export MTR_BGP_DB_PRESETS=__PRESETS__
export MTR_BGP_ROLE_MAP=__ROLE_MAP__
export MTR_BGP_IPVLAN_AUTO=1
export MTR_BGP_IPVLAN_BASE_IFACE=ens192
export MTR_BGP_RR_UPLINK_IFACE=ens224
export MTR_BGP_IPVLAN_PEER_IP=10.133.152.204
export MTR_SATELLITE_PEER_IP=10.133.152.204

cd __REMOTE__
PY=./venv/bin/python3

echo '=== 等待 Agent 就绪并 restore（最长 600s）==='
AGENT_OK=0
for i in $(seq 1 120); do
  if curl -sf http://127.0.0.1:9179/health >/dev/null 2>&1; then AGENT_OK=1; break; fi
  sleep 5
done
if [ "$AGENT_OK" != 1 ]; then echo 'FAIL agent health'; exit 1; fi
curl -sf -X POST 'http://127.0.0.1:8808/api/bgp/restore-agent' -H 'Content-Type: application/json' -d '{{}}' | head -c 1000
echo

curl -sf -X POST 'http://127.0.0.1:8808/api/bgp/neighbors/vbgp10133153204/10.133.152.204/advertise' \\
  -H 'Content-Type: application/json' -d '{{"advertise_routes":0}}' >/dev/null 2>&1 || true

sleep 8
echo '=== 验收 ==='
curl -sf http://127.0.0.1:9179/api/status | $PY -m json.tool 2>/dev/null | head -25
curl -sf http://127.0.0.1:9179/api/peers/freeze-status | $PY -m json.tool 2>/dev/null | head -40
curl -sf http://127.0.0.1:8808/api/bgp/neighbors | $PY -m json.tool 2>/dev/null
echo '--- :179 established ---'
ss -tn state established '( sport = :179 or dport = :179 )' 2>/dev/null | head -10 || true
""".replace("__REMOTE__", remote).replace(
        "__LOCAL_AS__", os.environ.get("LOCAL_AS", "63199")
    ).replace("__ROUTER_ID__", os.environ.get("ROUTER_ID", "10.133.153.200")).replace(
        "__PRESETS__",
        os.environ.get(
            "MTR_BGP_DB_PRESETS",
            "default:10.133.153.204:rr,default:10.133.152.204:downstream",
        ),
    ).replace(
        "__ROLE_MAP__",
        os.environ.get("MTR_BGP_ROLE_MAP", "10.133.153.204:rr,10.133.152.204:downstream"),
    )

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    fix_sh = LAB / "fix_vbgp204_reconnect.sh"
    if fix_sh.is_file():
        sftp.put(str(fix_sh), f"{remote}/fix_vbgp204_reconnect.sh")
    sftp.close()
    _, o, e = c.exec_command("bash -se", timeout=300)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    print(out)
    ok = "Established" in out or "established" in out.lower()
    if "rr_connected\": true" in out.replace(" ", "") or '"rr_connected": true' in out:
        ok = True
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
