#!/usr/bin/env python3
"""
现网 109：外网 mtr/traceroute 隐身（仅从上联口进入的转发流量）。

- mangle PREROUTING -i <上联>: TTL --ttl-inc 1 → 外网进向转发不在 109 单独占一跳（不限目的网段）
- OUTPUT：本机管理/数据面源地址对非运维白名单不回 ICMP（防 Echo Reply / TE 露 IP）

用法（仓库根目录，已配置 109/env）：
  python 109/apply_inbound_trace_stealth.py          # 下发 + 自检
  python 109/apply_inbound_trace_stealth.py --check
  python 109/apply_inbound_trace_stealth.py --teardown   # 回退

持久化：/usr/local/sbin/mtr-op-inbound-trace-stealth.sh（重启后需自行执行，无 systemd 单元）

验证（外网）：
  mtr -r -c 5 <内网IP>  → 路径中不应出现 101.89.68.109 / 43.207；原 109 位不应单独一行 ???
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

DEFAULT_ADMIN_CIDRS = (
    "101.251.211.168/29,"
    "101.251.214.176/28,"
    "106.120.247.120/29,"
    "101.251.204.16/29,"
    "164.52.12.80/29,"
    "101.251.255.176/29,"
    "127.0.0.0/8"
)


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
    up = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0")
    mgmt = os.environ.get("MTR_INBOUND_STEALTH_MGMT_IP", "101.89.68.109")
    router_id = os.environ.get("ROUTER_ID", "139.159.43.207")
    down_addr = os.environ.get("MTR_DOWNSTREAM_ADDR", "139.159.43.209/24")
    # 209/24 → 209
    down_ip = down_addr.split("/")[0].strip() if down_addr else "139.159.43.209"
    admin = os.environ.get("MTR_ADMIN_ACL_CIDRS", DEFAULT_ADMIN_CIDRS).strip()
    persist = os.environ.get(
        "MTR_INBOUND_STEALTH_PERSIST",
        "/usr/local/sbin/mtr-op-inbound-trace-stealth.sh",
    )
    ttl_inc = os.environ.get("MTR_INBOUND_STEALTH_TTL_INC", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    icmp_drop = os.environ.get("MTR_INBOUND_STEALTH_ICMP_DROP", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    chain = os.environ.get("MTR_INBOUND_STEALTH_CHAIN", "MTR_STEALTH_OUT")

    return f"""
set -e
MODE={mode!r}
UP={up!r}
MGMT={mgmt!r}
ROUTER_ID={router_id!r}
DOWN_IP={down_ip!r}
ADMIN_RAW={admin!r}
PERSIST={persist!r}
CHAIN={chain!r}
TTL_INC={'1' if ttl_inc else '0'}
ICMP_DROP={'1' if icmp_drop else '0'}

modprobe xt_TTL 2>/dev/null || modprobe ipt_ttl 2>/dev/null || true

teardown_ttl_legacy_forward() {{
  while iptables -t mangle -C FORWARD -i "$UP" -j TTL --ttl-inc 1 2>/dev/null; do
    iptables -t mangle -D FORWARD -i "$UP" -j TTL --ttl-inc 1
    echo "removed legacy FORWARD ttl-inc"
  done
}}

flush_stealth_out() {{
  for SRC in "$MGMT" "$ROUTER_ID" "$DOWN_IP"; do
    while iptables -C OUTPUT -p icmp -s "$SRC" -j "$CHAIN" 2>/dev/null; do
      iptables -D OUTPUT -p icmp -s "$SRC" -j "$CHAIN"
    done
  done
  iptables -F "$CHAIN" 2>/dev/null || true
  iptables -X "$CHAIN" 2>/dev/null || true
}}

apply_stealth_out() {{
  flush_stealth_out
  iptables -N "$CHAIN"
  # 运维白名单：仍允许 ping 管理/数据面
  OLDIFS="$IFS"
  IFS=,
  for cidr in $ADMIN_RAW; do
    cidr="${{cidr// /}}"
    [ -n "$cidr" ] || continue
    iptables -A "$CHAIN" -d "$cidr" -j RETURN
  done
  IFS="$OLDIFS"
  iptables -A "$CHAIN" -j DROP
  for SRC in "$MGMT" "$ROUTER_ID" "$DOWN_IP"; do
    iptables -A OUTPUT -p icmp -s "$SRC" -j "$CHAIN"
  done
}}

teardown_ttl() {{
  while iptables -t mangle -C PREROUTING -i "$UP" -j TTL --ttl-inc 1 2>/dev/null; do
    iptables -t mangle -D PREROUTING -i "$UP" -j TTL --ttl-inc 1
  done
  teardown_ttl_legacy_forward
}}

apply_ttl() {{
  teardown_ttl
  iptables -t mangle -I PREROUTING 1 -i "$UP" -j TTL --ttl-inc 1
}}

teardown_all() {{
  teardown_ttl
  if [ "$ICMP_DROP" = "1" ]; then flush_stealth_out; fi
  echo "teardown: inbound trace stealth removed"
}}

apply_all() {{
  if [ "$TTL_INC" = "1" ]; then apply_ttl; else teardown_ttl; fi
  if [ "$ICMP_DROP" = "1" ]; then apply_stealth_out; else flush_stealth_out; fi
  echo "apply: TTL_INC=$TTL_INC ICMP_DROP=$ICMP_DROP UP=$UP"
}}

write_persist() {{
  cat > "$PERSIST" <<'EOS'
#!/bin/bash
# mtr-op-inbound-trace-stealth — 由 109/apply_inbound_trace_stealth.py 生成
set -e
UP=__UP__
MGMT=__MGMT__
ROUTER_ID=__ROUTER_ID__
DOWN_IP=__DOWN_IP__
ADMIN_RAW=__ADMIN_RAW__
CHAIN=__CHAIN__
TTL_INC=__TTL_INC__
ICMP_DROP=__ICMP_DROP__
modprobe xt_TTL 2>/dev/null || true
# shellcheck disable=SC2120
teardown_ttl_legacy_forward() {{
  while iptables -t mangle -C FORWARD -i "$UP" -j TTL --ttl-inc 1 2>/dev/null; do
    iptables -t mangle -D FORWARD -i "$UP" -j TTL --ttl-inc 1
  done
}}
flush_stealth_out() {{
  for SRC in "$MGMT" "$ROUTER_ID" "$DOWN_IP"; do
    while iptables -C OUTPUT -p icmp -s "$SRC" -j "$CHAIN" 2>/dev/null; do
      iptables -D OUTPUT -p icmp -s "$SRC" -j "$CHAIN"
    done
  done
  iptables -F "$CHAIN" 2>/dev/null || true
  iptables -X "$CHAIN" 2>/dev/null || true
}}
apply_stealth_out() {{
  flush_stealth_out
  iptables -N "$CHAIN"
  IFS=,
  for cidr in $ADMIN_RAW; do
    cidr="${{cidr// /}}"
    [ -n "$cidr" ] || continue
    iptables -A "$CHAIN" -d "$cidr" -j RETURN
  done
  unset IFS
  iptables -A "$CHAIN" -j DROP
  for SRC in "$MGMT" "$ROUTER_ID" "$DOWN_IP"; do
    iptables -A OUTPUT -p icmp -s "$SRC" -j "$CHAIN"
  done
}}
teardown_ttl() {{
  while iptables -t mangle -C PREROUTING -i "$UP" -j TTL --ttl-inc 1 2>/dev/null; do
    iptables -t mangle -D PREROUTING -i "$UP" -j TTL --ttl-inc 1
  done
  teardown_ttl_legacy_forward
}}
apply_ttl() {{
  teardown_ttl
  iptables -t mangle -I PREROUTING 1 -i "$UP" -j TTL --ttl-inc 1
}}
if [ "$TTL_INC" = "1" ]; then apply_ttl; else teardown_ttl; fi
if [ "$ICMP_DROP" = "1" ]; then apply_stealth_out; else flush_stealth_out; fi
EOS
  sed -i "s|__UP__|$UP|g; s|__MGMT__|$MGMT|g; s|__ROUTER_ID__|$ROUTER_ID|g; s|__DOWN_IP__|$DOWN_IP|g; s|__ADMIN_RAW__|$ADMIN_RAW|g; s|__CHAIN__|$CHAIN|g; s|__TTL_INC__|$TTL_INC|g; s|__ICMP_DROP__|$ICMP_DROP|g" "$PERSIST"
  chmod +x "$PERSIST"
  echo "persist -> $PERSIST"
}}

verify() {{
  echo "=== mangle PREROUTING TTL (uplink in) ==="
  if [ "$TTL_INC" = "1" ]; then
    if iptables -t mangle -C PREROUTING -i "$UP" -j TTL --ttl-inc 1 2>/dev/null; then
      echo "OK prerouting ttl-inc present"
    else
      echo "FAIL missing prerouting ttl-inc"
    fi
  else
    echo "SKIP TTL_INC=0"
  fi
  iptables -t mangle -L PREROUTING -n -v | grep -E 'TTL|ttl' || true
  iptables -t mangle -L FORWARD -n -v | grep -E 'TTL|ttl' && echo "WARN legacy FORWARD ttl-inc still present" || true
  echo "=== OUTPUT stealth chain ==="
  if [ "$ICMP_DROP" = "1" ]; then
    iptables -L "$CHAIN" -n -v 2>/dev/null | head -20 || echo "FAIL no chain $CHAIN"
    iptables -L OUTPUT -n -v | grep "$CHAIN" || echo "WARN no jumps to $CHAIN"
  else
    echo "SKIP ICMP_DROP=0"
  fi
  echo "=== sample route (any dest via uplink in) ==="
  ip route get 139.159.105.94 iif "$UP" 2>&1 | head -1 || true
  echo "=== external verify hint ==="
  echo "From outside: mtr -r -c 5 <internal-ip>"
  echo "Expect: no $MGMT / $ROUTER_ID hop; no extra ??? row at old OP position; last hop = target"
}}

case "$MODE" in
  teardown)
    teardown_all
    ;;
  check)
    verify
    ;;
  *)
    apply_all
    write_persist
    verify
    ;;
esac
"""


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description="109 外网进向 mtr 隐身（TTL-inc + ICMP drop）")
    ap.add_argument("--check", action="store_true", help="仅检查规则是否存在")
    ap.add_argument("--teardown", action="store_true", help="回退：删除 TTL-inc 与 OUTPUT 链")
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
    stdin, stdout, stderr = client.exec_command("bash -se", timeout=90)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    client.close()
    print(out)
    if err.strip():
        print(err, file=sys.stderr)
    return rc if rc != 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
