#!/usr/bin/env python3
import os
import sys
from pathlib import Path
import paramiko

DEPLOY = Path(__file__).resolve().parent
name = sys.argv[1] if len(sys.argv) > 1 else "_why_peer_no_recv.sh"

for line in (DEPLOY / "env.example").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(os.environ["MTR_OP_HOST"], username="root", password=os.environ["MTR_OP_SSH_PASSWORD"], timeout=30)
sftp = c.open_sftp()
sftp.put(str(DEPLOY / name), f"/tmp/{name}")
sftp.close()
i, o, e = c.exec_command(f"bash /tmp/{name}", timeout=120)
print(o.read().decode(errors="replace") + e.read().decode(errors="replace"))
c.close()
