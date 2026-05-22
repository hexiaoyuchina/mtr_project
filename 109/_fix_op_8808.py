#!/usr/bin/env python3
"""Enable and start mtr-op on 109 after reboot."""
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
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
    host = os.environ["MTR_OP_HOST"]
    pw = os.environ["MTR_OP_SSH_PASSWORD"]
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

    unit_src = ROOT / "service" / "systemd" / "mtr-op.service"
    if unit_src.is_file():
        sftp = c.open_sftp()
        try:
            sftp.put(str(unit_src), "/etc/systemd/system/mtr-op.service")
            print("uploaded mtr-op.service")
        finally:
            sftp.close()

    # 上传 main.py 启动短超时补丁（若本地有）
    main_py = ROOT / "service" / "app" / "main.py"
    if main_py.is_file():
        sftp = c.open_sftp()
        try:
            sftp.put(str(main_py), f"{remote}/app/main.py")
            print(f"uploaded {remote}/app/main.py")
        finally:
            sftp.close()

    script = f"""set -e
REMOTE={remote}
mkdir -p /etc/systemd/system/mtr-op.service.d
cat > /etc/systemd/system/mtr-op.service.d/override.conf <<'EOF'
[Service]
Environment=GOBGP_AGENT_URL=http://127.0.0.1:9179
Environment=MTR_BGP_AGENT_HTTP_TIMEOUT=8
Environment=MTR_BGP_STARTUP_SEED_TIMEOUT=8
Environment=MTR_BGP_STARTUP_RESTORE=0
Environment=MTR_BGP_RESUME_ADVERTISE=0
EOF

pkill -f 'uvicorn app.main:app' 2>/dev/null || true
sleep 1
systemctl daemon-reload
systemctl enable mtr-op
systemctl restart mtr-op
sleep 6
systemctl is-active mtr-op
ss -tlnp | grep ':8808' || true
for i in $(seq 1 20); do
  if curl -sf http://127.0.0.1:8808/health >/dev/null; then
    echo health_ok
    curl -sS http://127.0.0.1:8808/health
    echo
    break
  fi
  sleep 2
done
curl -sf http://127.0.0.1:8808/health >/dev/null || {{
  journalctl -u mtr-op -n 25 --no-pager
  exit 1
}}
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=120)
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
    print("fix_op_8808_ok")


if __name__ == "__main__":
    main()
