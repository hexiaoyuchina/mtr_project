#!/usr/bin/env python3
"""
Linux 200 实验室验收（SSH + HTTP）。不修改仓库其它目录。

用法：python 200/verify.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

LAB_DIR = Path(__file__).resolve().parent


def load_lab_env() -> None:
    env_file = LAB_DIR / "lab.env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()


def bash(c: paramiko.SSHClient, script: str, timeout: int = 120) -> tuple[int, str]:
    i, o, e = c.exec_command("bash -se", timeout=timeout)
    i.write(script)
    i.channel.shutdown_write()
    out = o.read().decode(errors="replace") + e.read().decode(errors="replace")
    return o.channel.recv_exit_status(), out


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> None:
    load_lab_env()
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    user = os.environ.get("MTR_OP_SSH_USER", "root")
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        print("缺少 MTR_OP_SSH_PASSWORD", file=sys.stderr)
        sys.exit(2)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        host,
        username=user,
        password=pw,
        timeout=25,
        allow_agent=False,
        look_for_keys=False,
    )

    all_ok = True
    print(f"=== 验收 Linux 200 @ {host} ===\n")

    code, out = bash(
        c,
        """
set -e
echo '--- interfaces ---'
ip -br addr show ens160 ens192 ens224 2>/dev/null || true
echo '--- services ---'
systemctl is-active bgp-agent redis-server 2>/dev/null || true
pgrep -af 'uvicorn app.main|bgp_agent' | head -4
echo '--- agent ---'
curl -sf http://127.0.0.1:9179/health && echo
curl -s http://127.0.0.1:9179/api/status
echo
curl -s http://127.0.0.1:9179/api/peers/freeze-status
echo
echo '--- op ---'
curl -s http://127.0.0.1:8808/health && echo
curl -s http://127.0.0.1:8808/api/gobgp/status | head -c 600
echo
echo '--- bgp tcp ---'
ss -tnp state established 2>/dev/null | grep -E ':179|:1790' || true
echo '--- ping ---'
ip vrf exec vrf2103 ping -c1 -W2 10.133.153.204 || true
ping -c1 -W2 10.133.152.204 || true
""",
    )
    print(out)

    try:
        status = json.loads(
            [ln for ln in out.splitlines() if ln.strip().startswith("{") and "local_as" in ln][0]
        )
        all_ok &= check(
            "agent local_as",
            status.get("rx", {}).get("local_as") == 63199,
            str(status.get("rx", {}).get("local_as")),
        )
        all_ok &= check(
            "RR AS",
            status.get("rx", {}).get("rr_as") == 63199,
            str(status.get("rx", {}).get("rr_as")),
        )
    except (IndexError, json.JSONDecodeError, KeyError):
        all_ok &= check("agent status json", False, "parse failed")

    freeze_est = '"established":true' in out.replace(" ", "").lower()
    ss_est = "ESTABLISHED" in out or ":179" in out
    all_ok &= check("BGP sessions", freeze_est or ss_est, "freeze or ss :179")

    agent_active = False
    for ln in out.splitlines():
        if "bgp-agent" in ln and "systemctl" not in ln:
            agent_active = "active" in ln.lower()
            break
    if not agent_active:
        agent_active = "systemctl is-active bgp-agent" in out or (
            "active" in out and "177" in out and "bgp_agent" in out
        )
    all_ok &= check("bgp-agent running", agent_active or "bgp_agent" in out)

    all_ok &= check("8808 health", '"status":"ok"' in out.replace(" ", ""))

    c.close()
    print()
    if all_ok:
        print("verify_200_ok")
        sys.exit(0)
    print("verify_200_failed")
    sys.exit(1)


if __name__ == "__main__":
    main()
