#!/bin/bash
# Linux 201：业务源 10.133.152.204 → ens192 → 10.133.152.200 → Linux 200 → …
# 含：iptables mangle MARK + ip rule fwmark / from（与 scripts/lab_apply_mtr152_path.py 一致）。
#
# 用法：root 执行   bash linux201_src152_policy_route.sh
#
# 撤销示例：
#   iptables -t mangle -D OUTPUT -j MTR152_OUT 2>/dev/null; iptables -t mangle -F MTR152_OUT; iptables -t mangle -X MTR152_OUT
#   ip rule del fwmark 2001 lookup 2001 2>/dev/null; ip rule del from 10.133.152.204 lookup 2001 2>/dev/null
#   ip route flush table 2001

set -euo pipefail

TABLE_ID=2001
MARK=2001
SRC=10.133.152.204
GW=10.133.152.200
DEV=ens192

for i in all default lo "$DEV" ens160; do
  sysctl -w "net.ipv4.conf.${i}.rp_filter=2" 2>/dev/null || true
done

iptables -t mangle -S OUTPUT 2>/dev/null | grep -q 'MTR152_OUT' && iptables -t mangle -D OUTPUT -j MTR152_OUT || true
iptables -t mangle -F MTR152_OUT 2>/dev/null || true
iptables -t mangle -X MTR152_OUT 2>/dev/null || true
iptables -t mangle -N MTR152_OUT
iptables -t mangle -A MTR152_OUT -s "$SRC" ! -d 10.133.152.0/24 -j MARK --set-mark "$MARK"
iptables -t mangle -A OUTPUT -j MTR152_OUT

ip route flush table "$TABLE_ID" 2>/dev/null || true
ip route add 10.133.152.0/24 dev "$DEV" scope link table "$TABLE_ID"
ip route add 10.133.153.0/24 via "$GW" dev "$DEV" table "$TABLE_ID"
ip route add default via "$GW" dev "$DEV" table "$TABLE_ID"

set +e
while ip rule del fwmark "$MARK" lookup "$TABLE_ID" 2>/dev/null; do :; done
while ip rule del from "$SRC" lookup "$TABLE_ID" 2>/dev/null; do :; done
set -e

ip rule add fwmark "$MARK" lookup "$TABLE_ID" pref 9
ip rule add from "$SRC" lookup "$TABLE_ID" pref 10

ip route flush cache 2>/dev/null || true

echo "--- ip rule ---"
ip rule list | grep -E "$SRC|fwmark.*$MARK" || true
echo "--- ip route table $TABLE_ID ---"
ip route show table "$TABLE_ID"
