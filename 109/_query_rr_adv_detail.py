#!/usr/bin/env python3
"""现网只读：RR 行通告任务 + 聚合源库样本 + gobgp adj-out nexthop 统计。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit(2)

DEPLOY = Path(__file__).resolve().parent
RR = "139.159.43.249"
DS = "139.159.43.208"
RR_VRF = "gobgp-rr"


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        if name == "env":
            break


def main() -> None:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        print("missing MTR_OP_SSH_PASSWORD", file=sys.stderr)
        sys.exit(2)

    script_path = DEPLOY / "_query_rr_adv_detail_remote.sh"
    remote_sh = script_path.read_text(encoding="utf-8")

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ.get("MTR_OP_HOST", "101.89.68.109"),
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=pw,
        timeout=30,
    )
    sftp = c.open_sftp()
    try:
        sftp.put(str(script_path), "/tmp/query_rr_adv_detail.sh")
    finally:
        sftp.close()
    i, o, e = c.exec_command("bash /tmp/query_rr_adv_detail.sh", timeout=300)
    out = o.read().decode(errors="replace") + e.read().decode(errors="replace")
    print(out)
    c.close()
    if o.channel.recv_exit_status() != 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
