#!/usr/bin/env python3
"""检查 200 上 OP / bgp-agent API 与健康状态，必要时尝试拉起。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent


def load_env() -> str:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def main() -> int:
    pw = load_env()
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    script = r"""
set -x
echo '=== systemd ==='
systemctl is-active mtr-op.service 2>/dev/null || echo mtr-op:unknown
systemctl is-active bgp-agent.service 2>/dev/null || echo bgp-agent:unknown
systemctl status mtr-op.service --no-pager -n 8 2>/dev/null | tail -12 || true
echo '---'
systemctl status bgp-agent.service --no-pager -n 8 2>/dev/null | tail -12 || true
echo '=== ports ==='
ss -tlnp | grep -E ':8808|:9179' || echo no_8808_9179
echo '=== curl health ==='
curl -s -m 5 -o /dev/null -w 'op_8808:%{http_code}\n' http://127.0.0.1:8808/health 2>/dev/null || echo op_8808:fail
curl -s -m 5 http://127.0.0.1:8808/health 2>/dev/null | head -c 200; echo
curl -s -m 5 -o /dev/null -w 'agent_9179:%{http_code}\n' http://127.0.0.1:9179/health 2>/dev/null || echo agent_9179:fail
curl -s -m 5 http://127.0.0.1:9179/health 2>/dev/null | head -c 200; echo
echo '=== recent logs ==='
journalctl -u mtr-op -n 15 --no-pager 2>/dev/null | tail -15
echo '---'
journalctl -u bgp-agent -n 15 --no-pager 2>/dev/null | tail -15
echo '=== try restart if down ==='
need=0
systemctl is-active mtr-op.service >/dev/null 2>&1 || need=1
systemctl is-active bgp-agent.service >/dev/null 2>&1 || need=1
curl -sf -m 3 http://127.0.0.1:8808/health >/dev/null 2>&1 || need=1
curl -sf -m 3 http://127.0.0.1:9179/health >/dev/null 2>&1 || need=1
if [ "$need" = 1 ]; then
  echo restarting...
  systemctl restart bgp-agent.service 2>/dev/null || true
  sleep 3
  systemctl restart mtr-op.service 2>/dev/null || true
  sleep 5
  systemctl is-active mtr-op.service bgp-agent.service 2>/dev/null || true
  curl -s -m 8 http://127.0.0.1:9179/health | head -c 120; echo
  curl -s -m 8 http://127.0.0.1:8808/health | head -c 120; echo
fi
"""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(
            host,
            username=os.environ.get("MTR_OP_SSH_USER", "root"),
            password=pw,
            timeout=45,
            allow_agent=False,
            look_for_keys=False,
            banner_timeout=45,
        )
    except Exception as e:
        print(f"SSH 连接 {host} 失败: {e}", file=sys.stderr)
        return 1
    _, o, e = c.exec_command("bash -se", timeout=120)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    print(out)
    return 0 if "op_8808:200" in out or '"status":"ok"' in out else 1


if __name__ == "__main__":
    raise SystemExit(main())
