#!/usr/bin/env python3
"""
现网 109：下联（ARP 冒充网关进入）转发，从上联出；不在上联 add/del 地址。

- table 2110（可环境变量 MTR_DOWNSTREAM_TRANSIT_TABLE）：208 回程 + default 经 RR(249) 从上联出去，src=ROUTER_ID(207)
- ip rule：仅 from 下游 peer iif 下联口 lookup 2110（pref 30），不碰卫星 from 冒充IP 的 1000+ 规则
- 主表 PEER/32 → 下联口：回程（外网→208 从上联进）须转发到 eno1np0，避免误走 43.0/24 上联直连

用法（仓库根或 109 目录）：
  python 109/apply_downstream_transit.py          # 下发
  python 109/apply_downstream_transit.py --check
  python 109/apply_downstream_transit.py --teardown
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

DIR = Path(__file__).resolve().parent


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


def remote_script(mode: str) -> str:
    down = os.environ.get("MTR_OP_DOWNSTREAM_IFACE", "eno1np0")
    up = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0")
    peer = os.environ.get("MTR_BGP_IPVLAN_PEER_IP", "139.159.43.208")
    rr = os.environ.get("RR_ADDR", "139.159.43.249")
    src = os.environ.get("ROUTER_ID", "139.159.43.207")
    table = os.environ.get("MTR_DOWNSTREAM_TRANSIT_TABLE", "2110")
    pref = os.environ.get("MTR_DOWNSTREAM_TRANSIT_RULE_PREF", "30")
    persist = os.environ.get(
        "MTR_DOWNSTREAM_TRANSIT_PERSIST",
        "/usr/local/sbin/mtr-op-downstream-transit.sh",
    )
    down_addr = os.environ.get("MTR_DOWNSTREAM_ADDR", "139.159.43.209/24")

    return f"""
set -e
MODE={mode!r}
DOWN={down!r}
UP={up!r}
PEER={peer!r}
RR={rr!r}
SRC={src!r}
TABLE={table!r}
PREF={pref!r}
PERSIST={persist!r}
DOWN_ADDR={down_addr!r}

teardown() {{
  ip -4 rule del pref "$PREF" 2>/dev/null || true
  ip route flush table "$TABLE" 2>/dev/null || true
  ip route del "$PEER/32" dev "$DOWN" 2>/dev/null || true
  if [ -n "$DOWN_ADDR" ]; then
    ip addr del "$DOWN_ADDR" dev "$DOWN" 2>/dev/null || true
  fi
  echo "teardown table $TABLE pref $PREF main $PEER/32 done"
}}

apply_downstream_addr() {{
  if [ -z "$DOWN_ADDR" ]; then
    return 0
  fi
  ip addr show dev "$DOWN" | grep -q "${{DOWN_ADDR%%/*}}" && return 0
  ip addr add "$DOWN_ADDR" dev "$DOWN" 2>/dev/null || ip addr replace "$DOWN_ADDR" dev "$DOWN"
}}

apply_routes() {{
  ip route replace table "$TABLE" "$PEER/32" dev "$DOWN" scope link
  ip route replace table "$TABLE" 139.159.43.0/24 dev "$DOWN" scope link
  ip route replace table "$TABLE" default via "$RR" dev "$UP" src "$SRC"
}}

apply_main_return() {{
  # 上联有 43.0/24 时，主表会把 208 判到 uplink；ICMP 回程必须走下联
  ip route replace "$PEER/32" dev "$DOWN" scope link
}}

apply_rule() {{
  ip -4 rule del pref "$PREF" 2>/dev/null || true
  ip -4 rule add pref "$PREF" from "$PEER/32" iif "$DOWN" lookup "$TABLE"
}}

verify() {{
  echo "=== sysctl forward ==="
  sysctl -n net.ipv4.ip_forward
  echo "=== table $TABLE ==="
  ip route show table "$TABLE"
  echo "=== rule pref $PREF ==="
  ip -4 rule list | grep -E "^$PREF:" || true
  echo "=== route get forward (peer -> internet) ==="
  ip route get 8.8.8.8 from "$PEER" iif "$DOWN" 2>&1 | head -2
  echo "=== route get return (internet -> peer, iif uplink) ==="
  ip route get "$PEER" from 8.8.8.8 iif "$UP" 2>&1 | head -2
  echo "=== main host route peer ==="
  ip route show table main | grep "$PEER" || true
  echo "=== route get spoof local (must NOT use table $TABLE) ==="
  ip route get 8.8.8.8 from "$RR" 2>&1 | head -2
  echo "=== downstream addr ==="
  ip -br addr show dev "$DOWN"
  echo "=== uplink addrs (no changes expected) ==="
  ip -br addr show dev "$UP"
}}

write_persist() {{
  cat > "$PERSIST" <<'EOS'
#!/bin/bash
# mtr-op downstream transit — re-apply after reboot (no ip addr on uplink)
DOWN=__DOWN__
UP=__UP__
PEER=__PEER__
RR=__RR__
SRC=__SRC__
TABLE=__TABLE__
PREF=__PREF__
DOWN_ADDR=__DOWN_ADDR__
[ -d "/sys/class/net/$DOWN" ] || exit 0
[ -d "/sys/class/net/$UP" ] || exit 0
if [ -n "$DOWN_ADDR" ]; then
  ip addr show dev "$DOWN" | grep -q "${{DOWN_ADDR%%/*}}" || ip addr add "$DOWN_ADDR" dev "$DOWN" 2>/dev/null || ip addr replace "$DOWN_ADDR" dev "$DOWN"
fi
ip route replace table "$TABLE" "$PEER/32" dev "$DOWN" scope link
ip route replace table "$TABLE" 139.159.43.0/24 dev "$DOWN" scope link
ip route replace table "$TABLE" default via "$RR" dev "$UP" src "$SRC"
ip route replace "$PEER/32" dev "$DOWN" scope link
ip -4 rule del pref "$PREF" 2>/dev/null || true
ip -4 rule add pref "$PREF" from "$PEER/32" iif "$DOWN" lookup "$TABLE"
EOS
  sed -i "s|__DOWN__|$DOWN|g; s|__UP__|$UP|g; s|__PEER__|$PEER|g; s|__RR__|$RR|g; s|__SRC__|$SRC|g; s|__TABLE__|$TABLE|g; s|__PREF__|$PREF|g; s|__DOWN_ADDR__|$DOWN_ADDR|g" "$PERSIST"
  chmod +x "$PERSIST"
  echo "persist -> $PERSIST"
}}

case "$MODE" in
  teardown) teardown ;;
  check) verify ;;
  *)
    apply_downstream_addr
    apply_routes
    apply_main_return
    apply_rule
    write_persist
    verify
    ;;
esac
"""


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--teardown", action="store_true")
    ap.add_argument("--host", default=os.environ.get("MTR_OP_HOST", "101.89.68.109"))
    args = ap.parse_args()

    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        print("set MTR_OP_SSH_PASSWORD or 109/env", file=sys.stderr)
        return 2

    mode = "teardown" if args.teardown else ("check" if args.check else "apply")
    script = remote_script(mode)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        args.host,
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=pw,
        timeout=25,
        allow_agent=False,
        look_for_keys=False,
    )
    stdin, stdout, stderr = client.exec_command("bash -se", timeout=60)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    client.close()
    print(out)
    if err.strip():
        print(err, file=sys.stderr)
    if mode == "check" and "8.8.8.8 from" in out and f"dev {os.environ.get('MTR_BGP_RR_UPLINK_IFACE', 'enp59s0f0np0')}" in out:
        return 0
    if mode == "apply" and "No route to host" in out:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
