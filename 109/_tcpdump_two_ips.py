#!/usr/bin/env python3
"""现网上联/下联抓包：139.159.105.93 与 8.8.8.8。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

DEPLOY = Path(__file__).resolve().parent
for name in ("env", "env.example"):
    p = DEPLOY / name
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        break

UPLINK = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0")
DOWN = os.environ.get("MTR_OP_DOWNSTREAM_IFACE", "eno1np0")
SEC = int(os.environ.get("TCPDUMP_SEC", "25"))
FILTER = "host 139.159.105.93 or host 8.8.8.8"

script = f"""set -e
UPLINK={UPLINK!r}
DOWN={DOWN!r}
FILTER={FILTER!r}
SEC={SEC}
echo "=== 接口 ==="
ip -br link show "$UPLINK" "$DOWN" 2>/dev/null || true
echo
echo "=== 后台 ping (触发流量) ==="
ping -c 4 -W 2 139.159.105.93 >/tmp/ping_10593.log 2>&1 &
ping -c 4 -W 2 8.8.8.8 >/tmp/ping_88.log 2>&1 &
sleep 1
echo "=== 上联 $UPLINK tcpdump ${{SEC}}s filter: $FILTER ==="
timeout "$SEC" tcpdump -ni "$UPLINK" -vv -c 80 "$FILTER" 2>&1 || true
echo
echo "=== 下联 $DOWN tcpdump ${{SEC}}s filter: $FILTER ==="
timeout "$SEC" tcpdump -ni "$DOWN" -vv -c 80 "$FILTER" 2>&1 || true
echo
echo "=== ping 结果 ==="
echo "--- 139.159.105.93 ---"
cat /tmp/ping_10593.log 2>/dev/null || true
echo "--- 8.8.8.8 ---"
cat /tmp/ping_88.log 2>/dev/null || true
wait 2>/dev/null || true
"""

def main() -> None:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    host = os.environ["MTR_OP_HOST"]
    user = os.environ.get("MTR_OP_SSH_USER", "root")
    pwd = os.environ["MTR_OP_SSH_PASSWORD"]
    print(f"SSH {user}@{host} uplink={UPLINK} down={DOWN} sec={SEC}", flush=True)
    c.connect(host, username=user, password=pwd, timeout=30)
    _, stdout, stderr = c.exec_command(f"bash -s <<'REMOTE_EOF'\n{script}\nREMOTE_EOF", timeout=SEC + 45)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    print(out)
    if err.strip():
        print(err, file=sys.stderr)
    c.close()


if __name__ == "__main__":
    main()
