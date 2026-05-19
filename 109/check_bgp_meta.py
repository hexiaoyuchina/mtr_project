#!/usr/bin/env python3
import os
from pathlib import Path
import paramiko

for line in Path(__file__).parent.joinpath("env.example").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

script = r"""
pid=$(pgrep -f 'uvicorn app.main' 2>/dev/null | head -1)
echo "uvicorn_pid=$pid"
if [ -n "$pid" ] && [ -r "/proc/$pid/environ" ]; then
  tr '\0' '\n' < "/proc/$pid/environ" | grep MTR_BGP_DB || echo "MTR_BGP_DB_PRESETS not in uvicorn env"
fi
echo "=== bgp_neighbor_meta ==="
sqlite3 /root/mtr_op/data.db "SELECT vrf,neighbor_ip,role,note FROM bgp_neighbor_meta ORDER BY vrf,neighbor_ip;"
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(
    os.environ["MTR_OP_HOST"],
    username="root",
    password=os.environ["MTR_OP_SSH_PASSWORD"],
    timeout=30,
    allow_agent=False,
    look_for_keys=False,
)
i, o, e = c.exec_command("bash -se", timeout=30)
i.write(script)
i.channel.shutdown_write()
print(o.read().decode(errors="replace"))
c.close()
