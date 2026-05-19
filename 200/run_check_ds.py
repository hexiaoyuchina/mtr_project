#!/usr/bin/env python3
import os
from pathlib import Path
import paramiko

for line in Path("200/lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()
pw = os.environ["MTR_OP_SSH_PASSWORD"]
script = Path("200/check_downstream_after_up.sh").read_text(encoding="utf-8")
for host in ("10.133.151.200", "10.133.151.201"):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=45)
    if host.endswith("200"):
        _, o, e = c.exec_command("bash -se", timeout=120)
        o.channel.send(script.encode())
        o.channel.shutdown_write()
    else:
        _, o, e = c.exec_command(
            "vtysh -c 'show bgp summary' 2>/dev/null | grep 153.204; ss -tnp | grep 153.204 | head -5",
            timeout=60,
        )
    print("===", host, "===")
    print((o.read() + e.read()).decode())
    c.close()
