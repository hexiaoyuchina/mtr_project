#!/usr/bin/env python3
import os
import posixpath
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
LAB = Path(__file__).parent

for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()

pw = os.environ["MTR_OP_SSH_PASSWORD"]
host = os.environ["MTR_OP_HOST"]
remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")

FILES = [
    "service/bgp_agent/api_ingest_bg.go",
    "service/bgp_agent/api_bidirectional.go",
    "service/bgp_agent/api_peer_rib.go",
    "service/bgp_agent/api_server.go",
    "service/bgp_agent/pkg/gobgp_path/adj_in.go",
    "service/bgp_agent/pkg/rx/learned_routes.go",
    "service/bgp_agent/pkg/storage/peer_rib.go",
    "service/app/bgp_peer_rib.py",
    "200/overlay/bgp_agent/api_bidirectional.go",
    "200/overlay/bgp_agent/pkg/tx/learned_routes.go",
]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
sftp = c.open_sftp()
for rel in FILES:
    p = ROOT / rel
    if not p.is_file():
        continue
    if rel.startswith("200/overlay"):
        rp = posixpath.join(remote, "bgp_agent", rel.split("overlay/bgp_agent/", 1)[1])
    elif rel.startswith("service/app"):
        rp = posixpath.join(remote, "app", rel.split("service/app/", 1)[1])
    else:
        rp = posixpath.join(remote, "bgp_agent", rel.split("service/bgp_agent/", 1)[1])
    sftp.put(str(p), rp)
    print("up", rp)
sftp.close()
_, o, e = c.exec_command(
    f"cd {remote}/bgp_agent && export PATH=/usr/local/go/bin:$PATH && go build -o bgp_agent . && systemctl restart bgp-agent && "
    f"systemctl restart mtr-op && sleep 3 && curl -sf http://127.0.0.1:9179/health && echo ok",
    timeout=180,
)
print((o.read() + e.read()).decode("utf-8", "replace"))
c.close()
