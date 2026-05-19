#!/usr/bin/env python3
import os
from pathlib import Path
import paramiko

pw = "1234qwer"
for line in (Path(__file__).parent / "lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()
pw = os.environ["MTR_OP_SSH_PASSWORD"]
remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")

script = f"""
set -e
echo '=== services ==='
systemctl is-active mtr-op bgp-agent 2>/dev/null || true
curl -sf http://127.0.0.1:8808/health && echo op_ok || echo op_FAIL
curl -sf http://127.0.0.1:9179/health && echo agent_ok || echo agent_FAIL
echo '=== neighbors API ==='
curl -s -w '\\nHTTP:%{{http_code}}\\n' http://127.0.0.1:8808/api/bgp/neighbors | head -c 2000
echo
echo '=== learned-routes API ==='
curl -s -w '\\nHTTP:%{{http_code}}\\n' 'http://127.0.0.1:8808/api/bgp/learned-routes?page=1&page_size=10' | head -c 1500
echo
echo '=== sqlite meta ==='
{remote}/venv/bin/python3 - <<'PY'
import sqlite3
conn = sqlite3.connect("{remote}/data.db")
conn.row_factory = sqlite3.Row
for row in conn.execute("SELECT vrf, neighbor_ip, role FROM bgp_neighbor_meta"):
    print(dict(row))
print("learned count", conn.execute("SELECT COUNT(*) FROM bgp_learned_routes").fetchone()[0])
conn.close()
PY
echo '=== op log tail ==='
tail -40 /tmp/mtr_op.log 2>/dev/null || journalctl -u mtr-op -n 30 --no-pager 2>/dev/null || true
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.133.151.200", username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
_, o, e = c.exec_command("bash -se", timeout=120)
o.channel.send(script.encode())
o.channel.shutdown_write()
print((o.read() + e.read()).decode("utf-8", "replace"))
c.close()
