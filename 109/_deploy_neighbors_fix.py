#!/usr/bin/env python3
import json
import os
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = Path(__file__).resolve().parent


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY_DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()
        if name == "env":
            break


def main() -> None:
    load_env()
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
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
    sftp = c.open_sftp()
    sftp.put(str(ROOT / "service" / "app" / "main.py"), f"{remote}/app/main.py")
    sftp.close()
    stdin, stdout, stderr = c.exec_command(
        "systemctl restart mtr-op && sleep 5 && "
        "curl -sf http://127.0.0.1:8808/api/bgp/neighbors",
        timeout=60,
    )
    body = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if err.strip():
        print(err)
    data = json.loads(body)
    print("neighbor_count=", len(data))
    for row in data:
        print(
            row["vrf"],
            row["neighbor_ip"],
            row["role"],
            row["session_state"],
            row.get("source_ip"),
        )
    c.close()


if __name__ == "__main__":
    main()
