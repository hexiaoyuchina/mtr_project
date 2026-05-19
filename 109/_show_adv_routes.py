#!/usr/bin/env python3
"""只读：展示当前聚合宣告出去的路由内容。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit(2)

DEPLOY = Path(__file__).resolve().parent

SCRIPT = r"""
set +e
PY=python3

echo "=== downstream peer rib (aggregate source) ==="
curl -sf "http://127.0.0.1:9179/api/rib/routes?window=downstream&vrf=vbgp13915943249&neighbor_ip=139.159.43.208&page=1&page_size=20" -o /tmp/ds_rib.json
$PY <<'PY'
import json
with open("/tmp/ds_rib.json") as f:
    raw = f.read()
if not raw.strip():
    print("(empty response)")
else:
    d = json.loads(raw)
    items = d.get("items") or d.get("routes") or []
    print("total:", d.get("total", len(items)))
    for x in items:
        print(json.dumps(x, ensure_ascii=False, indent=2))
PY

echo ""
echo "=== tx learned-routes ==="
curl -sf "http://127.0.0.1:9179/api/tx/learned-routes?vrf=vbgp13915943249" | $PY -m json.tool 2>/dev/null | head -40

echo ""
echo "=== op learned-routes ==="
curl -sf "http://127.0.0.1:8808/api/bgp/learned-routes?vrf=vbgp13915943249&neighbor_ip=139.159.43.208&page_size=10" | $PY -m json.tool 2>/dev/null | head -50

echo ""
echo "=== advertise task (what was sent to RR) ==="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors/gobgp-rr/139.159.43.249/advertise/status" | $PY -m json.tool 2>/dev/null
"""


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        if name == "env":
            break


def main() -> None:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        sys.exit(2)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ.get("MTR_OP_HOST", "101.89.68.109"),
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=pw,
        timeout=30,
    )
    i, o, e = c.exec_command("bash -se", timeout=90)
    i.write(SCRIPT)
    i.channel.shutdown_write()
    print(o.read().decode(errors="replace") + e.read().decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
