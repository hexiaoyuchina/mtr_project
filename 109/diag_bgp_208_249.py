#!/usr/bin/env python3
"""109 上排查 208<->249 下游 BGP 状态（只读，不改代码）。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

DEPLOY_DIR = Path(__file__).resolve().parent


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY_DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()
        if name == "env":
            break


def run(c: paramiko.SSHClient, script: str, timeout: int = 90) -> str:
    i, o, e = c.exec_command("bash -se", timeout=timeout)
    i.write(script)
    i.channel.shutdown_write()
    return o.read().decode(errors="replace") + e.read().decode(errors="replace")


def main() -> None:
    load_env()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109")
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        print("需要 109/env 中的 MTR_OP_SSH_PASSWORD", file=sys.stderr)
        raise SystemExit(2)

    script = r"""
set -e
PY=python3
command -v python3 >/dev/null || PY=python

echo "=== 1) Agent GET /api/neighbors (208 / 249 / vbgp43249) ==="
curl -sf http://127.0.0.1:9179/api/neighbors -o /tmp/ag_nb.json
$PY <<'PY'
import json
with open("/tmp/ag_nb.json") as f:
    d = json.load(f)
for n in d.get("neighbors") or []:
    a = str(n.get("address") or "")
    v = str(n.get("vrf") or "")
    if a in ("139.159.43.208", "139.159.43.249") or "43249" in v or "4324" in v:
        print(json.dumps(n, ensure_ascii=False, indent=2))
PY

echo ""
echo "=== 2) OP GET /api/bgp/neighbors?vrf=vbgp13915943249 ==="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors?vrf=vbgp13915943249" -o /tmp/op_nb.json
$PY <<'PY'
import json
with open("/tmp/op_nb.json") as f:
    rows = json.load(f)
for n in rows:
    print(json.dumps({
        "vrf": n.get("vrf"),
        "neighbor_ip": n.get("neighbor_ip"),
        "source_ip": n.get("source_ip"),
        "session_state": n.get("session_state"),
        "routes_received": n.get("routes_received"),
        "routes_sent": n.get("routes_sent"),
        "routes_cached": n.get("routes_cached"),
        "role": n.get("role"),
    }, ensure_ascii=False, indent=2))
PY

echo ""
echo "=== 3) Agent health (rx / processor) ==="
curl -sf http://127.0.0.1:9179/health | $PY -m json.tool 2>/dev/null | head -35

echo ""
echo "=== 4) TX learned routes vbgp13915943249 ==="
curl -sf "http://127.0.0.1:9179/api/tx/learned-routes?vrf=vbgp13915943249" -o /tmp/lr.json 2>/dev/null || echo "learned-routes HTTP fail"
$PY <<'PY'
import json
try:
    with open("/tmp/lr.json") as f:
        d = json.load(f)
    r = d.get("routes") or []
    print("route_count", len(r))
    for x in r[:8]:
        print(x)
except Exception as e:
    print("parse_err", e)
PY

echo ""
echo "=== 5) OP learned-routes (downstream window) ==="
curl -sf "http://127.0.0.1:8808/api/bgp/learned-routes?vrf=vbgp13915943249&neighbor_ip=139.159.43.208&page_size=5" -o /tmp/op_lr.json 2>/dev/null || echo "op learned-routes fail"
$PY <<'PY'
import json
try:
    with open("/tmp/op_lr.json") as f:
        d = json.load(f)
    items = d.get("items") or d.get("routes") or []
    print("items", len(items), "total", d.get("total"))
    for x in items[:5]:
        print(x)
except Exception as e:
    print("parse_err", e)
PY

echo ""
echo "=== 6) ss :179 / TX redirect ports ==="
ss -lntp 2>/dev/null | grep -E ':179|:18[0-3][0-9]' || true

echo ""
echo "=== 7) RR upstream (rx) peers ==="
curl -sf http://127.0.0.1:9179/api/neighbors -o /tmp/ag_nb2.json
$PY <<'PY'
import json
with open("/tmp/ag_nb2.json") as f:
    d = json.load(f)
rx = [n for n in (d.get("neighbors") or []) if str(n.get("session")).lower() == "rx"]
print("rx_count", len(rx))
for n in rx:
    print(json.dumps(n, ensure_ascii=False))
PY

echo ""
echo "=== 8) TCP 208 / kernel vrf routes (-d) ==="
ss -tnp | grep 139.159.43.208 || true
ip -d route show vrf vbgp13915943249 2>/dev/null | head -20

echo ""
echo "=== 9) bgp-agent journal (208/open/error) ==="
journalctl -u bgp-agent --since '40 min ago' --no-pager 2>/dev/null | grep -iE '208|open|error|fail|1830|249' | tail -30 || true
"""

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    try:
        print(run(c, script))
    finally:
        c.close()


if __name__ == "__main__":
    main()
