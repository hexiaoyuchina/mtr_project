#!/usr/bin/env python3
import os
import time
from pathlib import Path
import paramiko

pw = "1234qwer"
for line in (Path(__file__).parent / "lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()
pw = os.environ.get("MTR_OP_SSH_PASSWORD", pw)


def snap() -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.133.151.200", username="root", password=pw, timeout=20, allow_agent=False, look_for_keys=False)
    cmd = (
        "curl -sf 'http://127.0.0.1:9179/api/rib/routes/count?window=upstream&vrf=gobgp-rr&neighbor_ip=10.133.153.204'; "
        "echo; "
        "curl -sf http://127.0.0.1:9179/api/neighbors"
    )
    _, o, e = c.exec_command(cmd, timeout=30)
    raw = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    import json

    lines = raw.strip().splitlines()
    cnt = json.loads(lines[0]).get("count") if lines else 0
    pfx = 0
    for n in json.loads(raw[raw.find("{", 1) :]).get("neighbors", []):
        if n.get("address") == "10.133.153.204":
            pfx = n.get("pfx_rcd")
    return f"cached={cnt} pfx_rcd={pfx}"


for label in ("t0", "t+30s", "t+60s"):
    print(label, snap())
    if label != "t+60s":
        time.sleep(30)
