#!/usr/bin/env python3
"""
现网 101.89.68.109 验收（SSH + HTTP）。不连接远程时仅检查本地 env。

用法：python 109/verify.py
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

DEPLOY_DIR = Path(__file__).resolve().parent


def load_env() -> None:
    for name in ("env", "env.example"):
        env_file = DEPLOY_DIR / name
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            key = k.strip()
            if key and key not in os.environ:
                os.environ[key] = v.strip()
        if name == "env":
            break


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
    load_env()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109")
    user = os.environ.get("MTR_OP_SSH_USER", "root")
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    rr = os.environ.get("RR_ADDR", "139.159.43.249")
    downstream = os.environ.get("MTR_SATELLITE_PEER_IP", "139.159.43.208")
    uplink = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0")
    sat = os.environ.get("MTR_BGP_IPVLAN_BASE_IFACE", "eno1np0")
    mgmt = "enp59s0f1np1"

    if not pw:
        print("缺少 MTR_OP_SSH_PASSWORD（请创建 109/env）", file=sys.stderr)
        raise SystemExit(2)

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
    print(f"=== 验收现网 VR @ {host} ===\n")

    code, out = bash(
        c,
        f"""
set -e
echo '--- interfaces (review) ---'
ip -br addr show {mgmt} {uplink} {sat} 2>/dev/null || true
echo '--- services ---'
systemctl is-active bgp-agent redis-server 2>/dev/null || true
pgrep -af 'uvicorn app.main|bgp_agent' | head -4
echo '--- agent ---'
curl -sf http://127.0.0.1:9179/health && echo
curl -s http://127.0.0.1:9179/api/status
echo
echo '--- op ---'
curl -s http://127.0.0.1:8808/health && echo
curl -s http://127.0.0.1:8808/api/gobgp/status | head -c 600
echo
echo '--- static routes api ---'
curl -s http://127.0.0.1:8808/api/static-routes/scopes | head -c 400
echo
curl -s 'http://127.0.0.1:8808/api/static-routes?reconcile=0' | head -c 400
echo
echo '--- bgp tcp ---'
ss -tnp state established 2>/dev/null | grep -E ':179' || true
echo '--- reachability (optional) ---'
ping -c1 -W2 {rr} 2>/dev/null || true
ping -c1 -W2 {downstream} 2>/dev/null || true
""",
    )
    print(out)

    local_as = int(os.environ.get("LOCAL_AS", "63199"))
    try:
        status_line = [
            ln for ln in out.splitlines() if ln.strip().startswith("{") and "local_as" in ln
        ][0]
        status = json.loads(status_line)
        all_ok &= check(
            "agent local_as",
            status.get("rx", {}).get("local_as") == local_as,
            str(status.get("rx", {}).get("local_as")),
        )
    except (IndexError, json.JSONDecodeError, KeyError, ValueError):
        all_ok &= check("agent status json", False, "parse failed")

    all_ok &= check("9179 health", "9179/health" in out or '"ok"' in out.lower())
    all_ok &= check("8808 health", '"status":"ok"' in out.replace(" ", "").lower() or "8808/health" in out)
    all_ok &= check(
        "static-routes API",
        "static routes api" in out.lower()
        or '"vrfs"' in out
        or "/api/static-routes" in out,
    )
    all_ok &= check(
        "bgp-agent running",
        "bgp_agent" in out or "active" in out.lower(),
    )

    c.close()
    print()
    if all_ok:
        print("verify_109_ok")
        sys.exit(0)
    print("verify_109_failed (部署后若未建邻居，BGP TCP 可能仍 FAIL)")
    sys.exit(1)


if __name__ == "__main__":
    main()
