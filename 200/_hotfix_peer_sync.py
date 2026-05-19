#!/usr/bin/env python3
import os
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
for line in Path(__file__).with_name("lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

pw = os.environ["MTR_OP_SSH_PASSWORD"]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.133.151.200", username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
c.open_sftp().put(
    str(ROOT / "service" / "app" / "te_rewrite_peer_sync.py"),
    "/root/mtr_op/app/te_rewrite_peer_sync.py",
)
restart = f"""export MTR_OP_DB=/root/mtr_op/data.db
export MTR_TE_REWRITE_PEER_HOSTS=10.133.151.201
export MTR_OP_SSH_PASSWORD={pw!r}
pkill -f 'uvicorn app.main:app' 2>/dev/null || true
sleep 1
cd /root/mtr_op
nohup ./venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8808 >>/tmp/mtr_op.log 2>&1 &
sleep 5
curl -sf -X PUT http://127.0.0.1:8808/api/global -H 'Content-Type: application/json' -d '{{"hijack_enabled":true}}'
echo PUT_OK
grep te_rewrite_peer /tmp/mtr_op.log | tail -3
"""
_, o, e = c.exec_command("bash -se", timeout=60)
o.channel.send(restart.encode())
o.channel.shutdown_write()
print((o.read() + e.read()).decode())
c.close()
sys.exit(0)
