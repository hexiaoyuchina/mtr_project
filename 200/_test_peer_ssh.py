#!/usr/bin/env python3
import base64
import os
import shlex
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "service"))

from app.te_rewrite_peer_sync import _REMOTE_APPLY  # noqa: E402

for line in Path(__file__).with_name("lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

pw = os.environ["MTR_OP_SSH_PASSWORD"]
map_line = "10.131.61.1=100.100.100.100"
map_b64 = base64.b64encode(map_line.encode()).decode()
body = (
    f"export MTR_TE_REWRITE_SCRIPT=/root/te_rewrite_nfqueue.py\n"
    f"export MTR_TE_QUEUE_NUM=2\n"
    f"export MAP_B64={shlex.quote(map_b64)}\n"
    + _REMOTE_APPLY
)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.133.151.200", username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
script = f"""
export SSHPASS={shlex.quote(pw)}
/usr/bin/sshpass -e /usr/bin/ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@10.133.151.201 bash -se <<'REMOTE'
{body}
REMOTE
echo exit=$?
"""
_, o, e = c.exec_command("bash -se", timeout=60)
o.channel.send(script.encode())
o.channel.shutdown_write()
print((o.read() + e.read()).decode())
c.close()
