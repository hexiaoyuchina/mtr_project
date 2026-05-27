#!/usr/bin/env python3
"""
现网 109：下联进、上联出；回程对称：上联进 table 2111、下联出（不经管理口 default）。

- 去程 table 2110：iif 下联 lookup → default via RR 从上联出
- 回程 table 2111：105.92/30 dev 下联（勿 via 208）；pref 29 iif 上联 lookup 2111（与 pref30 对称，可 env 改回 to 网段）
- 客户端源 IP（如 105.94）静态邻居：MAC 取下联 peer(208) 的 lladdr，避免 ARP INCOMPLETE

用法：
  python 109/apply_downstream_transit.py
  python 109/apply_downstream_transit.py --check
  python 109/apply_downstream_transit.py --teardown

文档：docs/MTR_DOWNSTREAM_TRANSIT_109.md
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


def client_neigh_hosts() -> str:
    """空格分隔，用于 bash 循环；默认 105.94。"""
    raw = os.environ.get("MTR_DOWNSTREAM_CLIENT_NEIGH_HOSTS", "139.159.105.94")
    return " ".join(h.strip() for h in raw.split(",") if h.strip())


def remote_script(mode: str) -> str:
    down = os.environ.get("MTR_OP_DOWNSTREAM_IFACE", "eno1np0")
    up = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0")
    peer = os.environ.get("MTR_BGP_IPVLAN_PEER_IP", "139.159.43.208")
    rr = os.environ.get("RR_ADDR", "139.159.43.249")
    src = os.environ.get("ROUTER_ID", "139.159.43.207")
    fwd_table = os.environ.get("MTR_DOWNSTREAM_TRANSIT_TABLE", "2110")
    ret_table = os.environ.get("MTR_DOWNSTREAM_RETURN_TABLE", "2111")
    ret_prefix = os.environ.get("MTR_DOWNSTREAM_RETURN_PREFIX", "139.159.105.92/30")
    pref = os.environ.get("MTR_DOWNSTREAM_TRANSIT_RULE_PREF", "30")
    to_client_pref = os.environ.get("MTR_DOWNSTREAM_TO_CLIENT_RULE_PREF", "29")
    ret_rule_style = os.environ.get("MTR_DOWNSTREAM_RETURN_RULE_STYLE", "iif").strip().lower()
    persist = os.environ.get(
        "MTR_DOWNSTREAM_TRANSIT_PERSIST",
        "/usr/local/sbin/mtr-op-downstream-transit.sh",
    )
    stealth_persist = os.environ.get(
        "MTR_INBOUND_STEALTH_PERSIST",
        "/usr/local/sbin/mtr-op-inbound-trace-stealth.sh",
    )
    down_addr = os.environ.get("MTR_DOWNSTREAM_ADDR", "139.159.43.209/24")
    neigh_hosts = client_neigh_hosts()

    return f"""
set -e
MODE={mode!r}
DOWN={down!r}
UP={up!r}
PEER={peer!r}
RR={rr!r}
SRC={src!r}
FWD_TABLE={fwd_table!r}
RET_TABLE={ret_table!r}
RET_PREFIX={ret_prefix!r}
PREF={pref!r}
TO_CLIENT_PREF={to_client_pref!r}
RET_RULE_STYLE={ret_rule_style!r}
PERSIST={persist!r}
STEALTH_PERSIST={stealth_persist!r}
DOWN_ADDR={down_addr!r}
NEIGH_HOSTS={neigh_hosts!r}

purge_return_rules() {{
  while ip -4 rule del pref "$TO_CLIENT_PREF" to "$RET_PREFIX" lookup "$RET_TABLE" 2>/dev/null; do :; done
  while ip -4 rule del pref "$TO_CLIENT_PREF" iif "$UP" lookup "$RET_TABLE" 2>/dev/null; do :; done
  while ip -4 rule del pref "$TO_CLIENT_PREF" iif "$UP" to "$RET_PREFIX" lookup "$RET_TABLE" 2>/dev/null; do :; done
  while ip -4 rule del pref 31 iif "$UP" to "$RET_PREFIX" lookup "$RET_TABLE" 2>/dev/null; do :; done
}}

teardown_return() {{
  purge_return_rules
  for h in $NEIGH_HOSTS; do
    while ip -4 rule del pref "$TO_CLIENT_PREF" to "$h/32" lookup "$RET_TABLE" 2>/dev/null; do :; done
    while ip -4 rule del pref 31 iif "$UP" to "$h/32" lookup "$RET_TABLE" 2>/dev/null; do :; done
    while ip -4 rule del pref 31 iif "$UP" to "$h/32" lookup "$FWD_TABLE" 2>/dev/null; do :; done
    while ip -4 rule del pref "$TO_CLIENT_PREF" to "$h/32" lookup "$FWD_TABLE" 2>/dev/null; do :; done
    ip route del "$h/32" dev "$DOWN" table main 2>/dev/null || true
    ip neigh del "$h" dev "$DOWN" 2>/dev/null || true
  done
  ip route flush table "$RET_TABLE" 2>/dev/null || true
}}

teardown() {{
  ip -4 rule del pref "$PREF" 2>/dev/null || true
  teardown_return
  ip route flush table "$FWD_TABLE" 2>/dev/null || true
  ip route del "$PEER/32" dev "$DOWN" 2>/dev/null || true
  if [ -n "$DOWN_ADDR" ]; then
    ip addr del "$DOWN_ADDR" dev "$DOWN" 2>/dev/null || true
  fi
  echo "teardown fwd=$FWD_TABLE ret=$RET_TABLE done"
}}

apply_downstream_addr() {{
  if [ -z "$DOWN_ADDR" ]; then
    return 0
  fi
  ip addr show dev "$DOWN" | grep -q "${{DOWN_ADDR%%/*}}" && return 0
  ip addr add "$DOWN_ADDR" dev "$DOWN" 2>/dev/null || ip addr replace "$DOWN_ADDR" dev "$DOWN"
}}

apply_forward_routes() {{
  ip route replace table "$FWD_TABLE" "$PEER/32" dev "$DOWN" scope link
  ip route replace table "$FWD_TABLE" 139.159.43.0/24 dev "$UP" scope link
  ip route replace table "$FWD_TABLE" default via "$RR" dev "$UP" src "$SRC"
}}

apply_return_routes() {{
  ip route replace table "$RET_TABLE" "$RET_PREFIX" dev "$DOWN" scope link
  ip route replace table "$RET_TABLE" "$PEER/32" dev "$DOWN" scope link
}}

scrub_main_return_conflict() {{
  ip route del "$RET_PREFIX" dev "$DOWN" table main 2>/dev/null || true
  for h in $NEIGH_HOSTS; do
    ip route del "$h/32" dev "$DOWN" table main 2>/dev/null || true
    ip route del "$h/32" dev "$DOWN" 2>/dev/null || true
  done
}}

apply_main_return() {{
  ip route replace "$PEER/32" dev "$DOWN" scope link
}}

apply_forward_rule() {{
  ip -4 rule del pref "$PREF" 2>/dev/null || true
  while ip -4 rule del from "$PEER/32" iif "$DOWN" 2>/dev/null; do :; done
  ip -4 rule add pref "$PREF" iif "$DOWN" lookup "$FWD_TABLE"
}}

apply_return_rules() {{
  purge_return_rules
  case "$RET_RULE_STYLE" in
    to)
      ip -4 rule add pref "$TO_CLIENT_PREF" to "$RET_PREFIX" lookup "$RET_TABLE"
      ;;
    iif_to)
      ip -4 rule add pref "$TO_CLIENT_PREF" iif "$UP" to "$RET_PREFIX" lookup "$RET_TABLE"
      ;;
    iif|*)
      ip -4 rule add pref "$TO_CLIENT_PREF" iif "$UP" lookup "$RET_TABLE"
      ;;
  esac
}}

apply_client_neigh() {{
  MAC=$(ip neigh show dev "$DOWN" 2>/dev/null | awk -v p="$PEER" '$1 == p {{print $3; exit}}')
  if [ -z "$MAC" ] || [ "$MAC" = "FAILED" ] || [ "$MAC" = "INCOMPLETE" ]; then
    ping -c1 -W1 "$PEER" >/dev/null 2>&1 || true
    MAC=$(ip neigh show dev "$DOWN" 2>/dev/null | awk -v p="$PEER" '$1 == p {{print $3; exit}}')
  fi
  if [ -z "$MAC" ] || [ "$MAC" = "FAILED" ] || [ "$MAC" = "INCOMPLETE" ]; then
    echo "WARN: no lladdr for peer $PEER on $DOWN, skip client neigh"
    return 0
  fi
  for h in $NEIGH_HOSTS; do
    ip neigh replace "$h" lladdr "$MAC" dev "$DOWN" nud permanent
    echo "neigh: $h -> $MAC dev $DOWN (permanent)"
  done
}}

verify() {{
  echo "=== sysctl forward ==="
  sysctl -n net.ipv4.ip_forward
  echo "=== table $FWD_TABLE (forward) ==="
  ip route show table "$FWD_TABLE"
  echo "=== table $RET_TABLE (return) ==="
  ip route show table "$RET_TABLE"
  echo "=== rules 29/30 (return/forward) ==="
  ip -4 rule list | grep -E '^29:|^30:' || true
  echo "=== forward 105.94 iif $DOWN ==="
  ip route get 8.8.8.8 from 139.159.105.94 iif "$DOWN" 2>&1 | head -2
  echo "=== return 105.94 iif $UP ==="
  ip route get 139.159.105.94 from 8.8.8.8 iif "$UP" 2>&1 | head -2
  echo "=== local to 105.94 ==="
  ip route get 139.159.105.94 2>&1 | head -2
  echo "=== neigh $DOWN (peer + client) ==="
  ip neigh show dev "$DOWN" | grep -E '105\\.94|43\\.208' || true
}}

write_persist() {{
  cat > "$PERSIST" <<'EOS'
#!/bin/bash
DOWN=__DOWN__
UP=__UP__
PEER=__PEER__
RR=__RR__
SRC=__SRC__
FWD_TABLE=__FWD_TABLE__
RET_TABLE=__RET_TABLE__
RET_PREFIX=__RET_PREFIX__
PREF=__PREF__
TO_CLIENT_PREF=__TO_CLIENT_PREF__
RET_RULE_STYLE=__RET_RULE_STYLE__
DOWN_ADDR=__DOWN_ADDR__
NEIGH_HOSTS=__NEIGH_HOSTS__
purge_return_rules() {{
  while ip -4 rule del pref "$TO_CLIENT_PREF" to "$RET_PREFIX" lookup "$RET_TABLE" 2>/dev/null; do :; done
  while ip -4 rule del pref "$TO_CLIENT_PREF" iif "$UP" lookup "$RET_TABLE" 2>/dev/null; do :; done
  while ip -4 rule del pref "$TO_CLIENT_PREF" iif "$UP" to "$RET_PREFIX" lookup "$RET_TABLE" 2>/dev/null; do :; done
  while ip -4 rule del pref 31 iif "$UP" to "$RET_PREFIX" lookup "$RET_TABLE" 2>/dev/null; do :; done
}}
apply_return_rules() {{
  purge_return_rules
  case "$RET_RULE_STYLE" in
    to) ip -4 rule add pref "$TO_CLIENT_PREF" to "$RET_PREFIX" lookup "$RET_TABLE" ;;
    iif_to) ip -4 rule add pref "$TO_CLIENT_PREF" iif "$UP" to "$RET_PREFIX" lookup "$RET_TABLE" ;;
    iif|*) ip -4 rule add pref "$TO_CLIENT_PREF" iif "$UP" lookup "$RET_TABLE" ;;
  esac
}}
[ -d "/sys/class/net/$DOWN" ] || exit 0
[ -d "/sys/class/net/$UP" ] || exit 0
if [ -n "$DOWN_ADDR" ]; then
  ip addr show dev "$DOWN" | grep -q "${{DOWN_ADDR%%/*}}" || ip addr add "$DOWN_ADDR" dev "$DOWN" 2>/dev/null || ip addr replace "$DOWN_ADDR" dev "$DOWN"
fi
ip route replace table "$FWD_TABLE" "$PEER/32" dev "$DOWN" scope link
ip route replace table "$FWD_TABLE" 139.159.43.0/24 dev "$DOWN" scope link
ip route replace table "$FWD_TABLE" default via "$RR" dev "$UP" src "$SRC"
ip route replace table "$RET_TABLE" "$RET_PREFIX" dev "$DOWN" scope link
ip route replace table "$RET_TABLE" "$PEER/32" dev "$DOWN" scope link
ip route replace "$PEER/32" dev "$DOWN" scope link
ip -4 rule del pref "$PREF" 2>/dev/null || true
while ip -4 rule del from "$PEER/32" iif "$DOWN" 2>/dev/null; do :; done
ip -4 rule add pref "$PREF" iif "$DOWN" lookup "$FWD_TABLE"
purge_return_rules
apply_return_rules
MAC=$(ip neigh show dev "$DOWN" 2>/dev/null | awk -v p="$PEER" '$1 == p {{print $3; exit}}')
if [ -n "$MAC" ] && [ "$MAC" != "FAILED" ] && [ "$MAC" != "INCOMPLETE" ]; then
  for h in $NEIGH_HOSTS; do
    ip neigh replace "$h" lladdr "$MAC" dev "$DOWN" nud permanent
  done
fi
STEALTH=__STEALTH_PERSIST__
[ -x "$STEALTH" ] && "$STEALTH"
EOS
  sed -i "s|__DOWN__|$DOWN|g; s|__UP__|$UP|g; s|__PEER__|$PEER|g; s|__RR__|$RR|g; s|__SRC__|$SRC|g; s|__FWD_TABLE__|$fwd_table|g; s|__RET_TABLE__|$ret_table|g; s|__RET_PREFIX__|$ret_prefix|g; s|__PREF__|$pref|g; s|__TO_CLIENT_PREF__|$to_client_pref|g; s|__RET_RULE_STYLE__|$ret_rule_style|g; s|__DOWN_ADDR__|$down_addr|g; s|__NEIGH_HOSTS__|$neigh_hosts|g; s|__STEALTH_PERSIST__|$stealth_persist|g" "$PERSIST"
  chmod +x "$PERSIST"
  echo "persist -> $PERSIST"
}}

case "$MODE" in
  teardown) teardown ;;
  check) verify ;;
  *)
    teardown_return
    apply_downstream_addr
    apply_forward_routes
    apply_return_routes
    scrub_main_return_conflict
    apply_main_return
    apply_forward_rule
    apply_return_rules
    apply_client_neigh
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
    if mode == "apply" and "No route to host" in out:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
