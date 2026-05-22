#!/usr/bin/env python3
"""经 109 登录 208，源 139.159.105.94 跑 mtr，并在 109 上核对转发面。"""
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
PEER = "139.159.43.208"
SRC = "139.159.105.94"
DST = "8.8.8.8"
DOWN = os.environ.get("MTR_OP_DOWNSTREAM_IFACE", "eno1np0")
UP = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0")


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


def run_bash(c: paramiko.SSHClient, script: str, timeout: int = 180) -> tuple[int, str]:
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=timeout)
    stdout.channel.settimeout(timeout)
    stderr.channel.settimeout(timeout)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), out + err


def main() -> None:
    load_env()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    user = os.environ.get("MTR_OP_SSH_USER", "root").strip()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    peer_pw = os.environ.get("MTR_PEER_SSH_PASSWORD", pw).strip()
    if not pw:
        raise SystemExit("MTR_OP_SSH_PASSWORD required")

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"=== SSH {user}@{host} ===", flush=True)
    c.connect(
        host,
        username=user,
        password=pw,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )

    # 109 转发面快照
    snap = f"""
echo "========== 109 转发面（mtr 前）=========="
ip -4 rule list | grep -E '^29:|^30:' || true
echo "--- table 2110 ---"
ip route show table 2110
echo "--- table 2111 ---"
ip route show table 2111
ip route get {DST} from {SRC} iif {DOWN} 2>&1 | head -2
ip route get {SRC} from {DST} iif {UP} 2>&1 | head -2
ip neigh show dev {DOWN} | grep -E '105\\.94|43\\.208' || true
"""

    # 经 109 跳 208 执行 mtr（同网段，默认用 109 的密码试 208）
    mtr_block = f"""
echo "========== 208 地址与路由 =========="
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 -o BatchMode=no {user}@{PEER} 'bash -se' <<'REMOTE' || echo "SSH_208_FAILED exit=$?"
set -x
echo "--- addrs ---"
ip -br addr | grep -E '43\\.|105\\.' || ip -br addr
echo "--- has {SRC}? ---"
ip -4 addr show | grep -w {SRC} || echo "WARN: {SRC} not on local iface"
echo "--- route to {DST} ---"
ip route get {DST} from {SRC} 2>&1 | head -3
echo "--- mtr (src {SRC}) ---"
if command -v mtr >/dev/null 2>&1; then
  mtr -n -r -c 8 --local-address {SRC} {DST} 2>&1 || mtr -n -r -c 8 -a {SRC} {DST} 2>&1
else
  echo "mtr not installed, try traceroute"
  traceroute -n -s {SRC} -w 2 -q 1 -m 12 {DST} 2>&1 | head -20
fi
echo "--- ping from {SRC} ---"
ping -c 2 -W 2 -I {SRC} {DST} 2>&1 | tail -4
REMOTE
"""

    code, out = run_bash(c, snap + mtr_block, timeout=120)
    print(out, end="")

    # 若 ssh 密码交互失败，用 sshpass
    if "SSH_208_FAILED" in out or "Permission denied" in out:
        print("=== retry with sshpass ===", flush=True)
        retry = f"""
if command -v sshpass >/dev/null 2>&1; then
  sshpass -p {peer_pw!r} ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 {user}@{PEER} bash -se <<'REMOTE'
set -x
ip -4 addr show | grep -w {SRC} || echo "no {SRC}"
ip route get {DST} from {SRC} 2>&1 | head -2
mtr -n -r -c 8 --local-address {SRC} {DST} 2>&1 || mtr -n -r -c 8 -a {SRC} {DST} 2>&1
ping -c 2 -W 2 -I {SRC} {DST} 2>&1 | tail -3
REMOTE
else
  echo "sshpass missing; install sshpass or set MTR_PEER_SSH_PASSWORD"
fi
"""
        code2, out2 = run_bash(c, retry, timeout=120)
        print(out2, end="")
        code = code2 if code2 != 0 else code

    c.close()
    raise SystemExit(0 if code == 0 else code)


if __name__ == "__main__":
    main()
