#!/usr/bin/env python3
"""Upload arp_spoof_daemon and start missing processes on 109 (no uplink changes)."""
from __future__ import annotations

import os
from pathlib import Path

import paramiko

DEPLOY_DIR = Path(__file__).resolve().parent
ROOT = DEPLOY_DIR.parent


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
    host = os.environ["MTR_OP_HOST"]
    pw = os.environ["MTR_OP_SSH_PASSWORD"]
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
    local_daemon = ROOT / "scripts" / "arp_spoof_daemon.py"

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        host,
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=pw,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    sftp = c.open_sftp()
    try:
        try:
            sftp.stat(f"{remote}/scripts")
        except OSError:
            sftp.mkdir(f"{remote}/scripts")
        sftp.put(str(local_daemon), f"{remote}/scripts/arp_spoof_daemon.py")
        print(f"uploaded -> {remote}/scripts/arp_spoof_daemon.py")
    finally:
        sftp.close()

    script = f"""set -e
REMOTE={remote}
cd "$REMOTE"
if [ -x ./venv/bin/python ]; then PY=./venv/bin/python; else PY=python3; fi

echo '=== processes before ==='
pgrep -af 'bgp_agent|uvicorn app.main|te_rewrite|arp_spoof' || true

for svc in bgp-agent; do
  systemctl is-active "$svc" 2>/dev/null || systemctl start "$svc" 2>/dev/null || true
done

pgrep -f 'uvicorn app.main:app' >/dev/null || {{
  pkill -f 'uvicorn app.main:app' 2>/dev/null || true
  sleep 1
  : > /tmp/mtr_op.log
  nohup $PY -m uvicorn app.main:app --host 0.0.0.0 --port 8808 >> /tmp/mtr_op.log 2>&1 &
  sleep 4
}}

pkill -f mtr_spoof_nfqueue 2>/dev/null || true

pkill -f arp_spoof_daemon.py 2>/dev/null || true
sleep 1
: > /tmp/arp_spoof_daemon.log
nohup $PY scripts/arp_spoof_daemon.py --op-db "$REMOTE/data.db" --verbose >> /tmp/arp_spoof_daemon.log 2>&1 &
sleep 3

echo '=== processes after ==='
pgrep -af 'bgp_agent|uvicorn app.main|te_rewrite|arp_spoof' || true
echo '=== arp log ==='
tail -15 /tmp/arp_spoof_daemon.log 2>/dev/null || true
curl -sf http://127.0.0.1:9179/health && echo ' agent_ok'
curl -sf http://127.0.0.1:8808/health && echo ' op_ok'
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=90)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace")
    if err.strip():
        print("stderr:", err)
    code = stdout.channel.recv_exit_status()
    c.close()
    if code != 0:
        raise SystemExit(code)
    print("start_missing_services_ok")


if __name__ == "__main__":
    main()
