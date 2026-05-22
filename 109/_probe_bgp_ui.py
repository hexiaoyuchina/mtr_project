#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

import paramiko

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
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ["MTR_OP_HOST"],
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    script = r"""
uptime
systemctl is-active mtr-op bgp-agent
for url in \
  http://127.0.0.1:9179/health \
  http://127.0.0.1:9179/api/neighbors \
  http://127.0.0.1:8808/health \
  http://127.0.0.1:8808/api/bgp/neighbors; do
  echo "URL $url"
  curl -sS -o /dev/null -w '  code=%{http_code} time=%{time_total}s\n' -m 25 "$url" || echo '  FAIL'
done
journalctl -u mtr-op -n 6 --no-pager 2>/dev/null | tail -6
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=120)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print(stdout.read().decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
