#!/usr/bin/env python3
import json
import os
import urllib.request
from pathlib import Path

import paramiko

for line in Path(__file__).with_name("lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

pw = os.environ["MTR_OP_SSH_PASSWORD"]
base = "http://10.133.151.200:8808"

rules = json.load(urllib.request.urlopen(f"{base}/api/hop-rules", timeout=15))
print("=== hop-rules ===")
for x in rules:
    print(x.get("id"), x.get("match_cidr"), x.get("forged_src"), x.get("enabled"))
print("hijack", json.load(urllib.request.urlopen(f"{base}/api/global", timeout=10)))


def sh(host: str, cmd: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=20, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command(cmd, timeout=25)
    out = (o.read() + e.read()).decode()
    c.close()
    return out


for h in ("10.133.151.200", "10.133.151.201"):
    print(f"=== {h} ===")
    print(
        sh(
            h,
            "cat /tmp/mtr_te_map.env 2>/dev/null || echo NO_MAP; "
            "pgrep -af te_rewrite_nfqueue || echo NO_DAEMON; "
            "tail -3 /tmp/te_rewrite_nfqueue.log 2>/dev/null",
        )
    )
