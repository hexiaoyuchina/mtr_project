#!/usr/bin/env python3
import os
from pathlib import Path
import paramiko

pw = "1234qwer"
for line in (Path(__file__).parent / "lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()
pw = os.environ["MTR_OP_SSH_PASSWORD"]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.133.151.200", username="root", password=pw, timeout=20, allow_agent=False, look_for_keys=False)
_, o, e = c.exec_command(
    "systemctl is-active bgp-agent; curl -sf http://127.0.0.1:9179/health; echo; "
    "curl -sf 'http://127.0.0.1:9179/api/rib/routes/count?window=upstream&vrf=gobgp-rr&neighbor_ip=10.133.153.204'; echo; "
    "journalctl -u bgp-agent -n 8 --no-pager",
    timeout=30,
)
print((o.read() + e.read()).decode("utf-8", "replace"))
c.close()
