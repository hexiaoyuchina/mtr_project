#!/usr/bin/env python3
"""上传 te_rewrite peer 同步代码并重启 OP（实验室）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
LAB = Path(__file__).resolve().parent


def load_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not pw:
        print("need MTR_OP_SSH_PASSWORD", file=sys.stderr)
        return 2
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
    peer = os.environ.get("MTR_TE_REWRITE_PEER_HOSTS", "10.133.151.201")

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    for name in ("te_rewrite_sync.py", "te_rewrite_peer_sync.py"):
        sftp.put(str(ROOT / "service" / "app" / name), f"{remote}/app/{name}")
    sftp.put(str(ROOT / "scripts" / "te_rewrite_nfqueue.py"), f"{remote}/te_rewrite_nfqueue.py")
    sftp.close()

    script = f"""
set -e
apt-get install -y -qq sshpass 2>/dev/null || true
export MTR_OP_DB={remote}/data.db
export MTR_TE_REWRITE_PEER_HOSTS={peer}
export MTR_TE_REWRITE_PEER_QUEUE=2
export MTR_OP_SSH_PASSWORD={pw!r}
pkill -f 'uvicorn app.main:app' 2>/dev/null || true
sleep 1
cd {remote}
nohup python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8808 >>/tmp/mtr_op.log 2>&1 &
sleep 5
curl -sf http://127.0.0.1:8808/health && echo health_ok
curl -sf -X PATCH http://127.0.0.1:8808/api/hop-rules/2 \\
  -H 'Content-Type: application/json' \\
  -d '{{"forged_src":"200.200.200.200","enabled":true}}' >/dev/null && echo patch_ok
sleep 2
SSHPASS={pw!r} sshpass -e ssh -o StrictHostKeyChecking=no root@10.133.151.201 \\
  'cat /tmp/mtr_te_map.env; tail -1 /tmp/te_rewrite_nfqueue.log'
"""
    _, o, e = c.exec_command("bash -se", timeout=90)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode()
    c.close()
    print(out)
    return 0 if "151.210=200.200.200.200" in out else 1


if __name__ == "__main__":
    raise SystemExit(main())
