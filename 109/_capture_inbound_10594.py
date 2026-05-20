#!/usr/bin/env python3
"""109 上抓 host 139.159.105.94 全量流量，分析公网入向 mtr 走向。"""
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

SRC = "139.159.105.94"
DOWN = os.environ.get("MTR_OP_DOWNSTREAM_IFACE", "eno1np0")
UP = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0")
MGMT = "enp59s0f1np1"
SEC = int(os.environ.get("TCPDUMP_SEC", "35"))

script = f"""set -e
SRC={SRC!r}
DOWN={DOWN!r}
UP={UP!r}
MGMT={MGMT!r}
SEC={SEC}
echo "========== $(date -Is) host $SRC ${{SEC}}s =========="
echo "=== 路由 ==="
ip route get "$SRC" 2>&1 | head -1
ip route get "$SRC" from 8.8.8.8 iif "$UP" 2>&1 | head -1
ip route get 8.8.8.8 from "$SRC" iif "$DOWN" 2>&1 | head -1
echo "=== 邻居 ==="
ip neigh show dev "$DOWN" | grep -E '105\\.9|43\\.208' || true
echo
for dev in "$DOWN" "$UP" "$MGMT"; do
  timeout "$SEC" tcpdump -ni "$dev" -l -n "host $SRC" 2>/dev/null >"/tmp/cap_$dev.txt" &
done
wait 2>/dev/null || true
echo "=== 各口统计 ==="
for dev in "$DOWN" "$UP" "$MGMT"; do
  f="/tmp/cap_$dev.txt"
  echo "--- $dev ---"
  if [ ! -s "$f" ]; then echo "  (无包)"; continue; fi
  wc -l < "$f" | xargs echo "  行数:"
  grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+ > [0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+' "$f" | sort | uniq -c | sort -rn | head -15
  echo -n "  echo req: "; grep -ci 'echo request' "$f" || echo 0
  echo -n "  echo rep:  "; grep -ci 'echo reply' "$f" || echo 0
  echo -n "  time exc: "; grep -ci 'time exceeded' "$f" || echo 0
  echo "  样例:"
  head -10 "$f" | sed 's/^/    /'
done
echo
echo "=== 入向(目的=$SRC) ==="
for dev in "$DOWN" "$UP" "$MGMT"; do
  n=$(grep -cE "> $SRC" "/tmp/cap_$dev.txt" 2>/dev/null || echo 0)
  echo "  $dev: $n"
done
echo "=== 出向(源=$SRC) ==="
for dev in "$DOWN" "$UP" "$MGMT"; do
  n=$(grep -cE "^[^ ]+ [^ ]+ IP.* $SRC >" "/tmp/cap_$dev.txt" 2>/dev/null || grep -c "$SRC >" "/tmp/cap_$dev.txt" 2>/dev/null || echo 0)
  echo "  $dev: $n"
done
"""


def main() -> None:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    host = os.environ["MTR_OP_HOST"]
    user = os.environ.get("MTR_OP_SSH_USER", "root")
    pwd = os.environ["MTR_OP_SSH_PASSWORD"]
    print(f"SSH {user}@{host} capture host {SRC} {SEC}s", flush=True)
    c.connect(host, username=user, password=pwd, timeout=30)
    _, stdout, stderr = c.exec_command(
        f"bash -s <<'REMOTE_EOF'\n{script}\nREMOTE_EOF", timeout=SEC + 60
    )
    print(stdout.read().decode("utf-8", errors="replace"))
    err = stderr.read().decode("utf-8", errors="replace")
    if err.strip():
        print(err, file=sys.stderr)
    c.close()


if __name__ == "__main__":
    main()
