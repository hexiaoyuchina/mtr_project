#!/usr/bin/env python3
"""在 109 安装 Netdata，历史数据保留约 1 天；Web 默认 :19999。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

DIR = Path(__file__).resolve().parent
LOCAL_CONF = DIR / "netdata-retention-1d.conf"
REMOTE_CONF = "/etc/netdata/conf.d/mtr-retention-1d.conf"
KICKSTART = "/tmp/netdata-kickstart.sh"


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
        break


def connect() -> paramiko.SSHClient:
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    user = os.environ.get("MTR_OP_SSH_USER", "root").strip()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not pw:
        print("请配置 109/env 中 MTR_OP_SSH_PASSWORD", file=sys.stderr)
        raise SystemExit(2)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        host,
        username=user,
        password=pw,
        timeout=30,
        banner_timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    return c


def run(c: paramiko.SSHClient, script: str, timeout: int = 120) -> tuple[int, str]:
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=timeout)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), out


def main() -> None:
    load_env()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    local_install = DIR / "install_netdata_remote.sh"
    if not local_install.is_file():
        print(f"missing {local_install}", file=sys.stderr)
        raise SystemExit(1)

    # 避免与上次未完成的 kickstart 冲突
    bootstrap = """
set -euo pipefail
if pgrep -f 'netdata-kickstart.sh' >/dev/null 2>&1; then
  echo "kickstart_already_running"
  exit 0
fi
if [[ -f /root/netdata_install.done ]]; then
  echo "reconcile_config"
  bash /root/install_netdata_remote.sh
  exit 0
fi
nohup bash /root/install_netdata_remote.sh >/dev/null 2>&1 &
echo "install_started_pid=$!"
"""

    print(f"Connecting {host} ...", flush=True)
    c = connect()
    try:
        sftp = c.open_sftp()
        sftp.put(str(local_install), "/root/install_netdata_remote.sh")
        sftp.chmod("/root/install_netdata_remote.sh", 0o755)
        sftp.close()

        code, out = run(c, bootstrap, timeout=60)
        print(out)
        if code != 0:
            raise SystemExit(code)

        import time

        for i in range(60):
            time.sleep(30)
            code, out = run(
                c,
                """
if [[ -f /root/netdata_install.done ]]; then
  echo done
  tail -20 /root/netdata_install.log
  exit 0
fi
echo waiting
tail -3 /root/netdata_install.log 2>/dev/null || true
pgrep -af 'kickstart|apt-get.*netdata' || true
exit 1
""",
                timeout=45,
            )
            print(f"[poll {i+1}] {out.strip()[:500]}", flush=True)
            if code == 0 and "done" in out:
                print(f"\nweb (public): http://{host}:19999", flush=True)
                print("ensure cloud SG / upstream firewall allows TCP 19999", flush=True)
                print("retention: dbengine tier 0 = 1d (see /etc/netdata/conf.d/mtr-retention-1d.conf)", flush=True)
                print("deploy_netdata_109_ok", flush=True)
                return
        print("install still running; check: tail -f /root/netdata_install.log", file=sys.stderr)
        raise SystemExit(2)
    finally:
        c.close()


if __name__ == "__main__":
    main()
