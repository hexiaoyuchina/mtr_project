#!/bin/bash
set -uo pipefail
MATCH=148.153.127.105
FORGE=200.200.200.200
DOWN="${MTR_OP_DOWNSTREAM_IFACE:-eno1np0}"
UP="${MTR_BGP_RR_UPLINK_IFACE:-enp59s0f0np0}"
MGMT="${MTR_OP_MGMT_IFACE:-enp59s0f1np1}"

echo "========== 1) OP hop_replace_rules (API) =========="
curl -sf http://127.0.0.1:9179/api/hop-rules 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
for r in d:
    print(f\"  id={r.get('id')} pri={r.get('priority')} en={r.get('enabled')} match={r.get('match_cidr')} -> {r.get('forged_src')} note={r.get('note','')}\")
" 2>/dev/null || echo "  (API failed)"

echo
echo "========== 2) TE map / daemon =========="
grep -E 'MTR_TE_REWRITE_MAP|148\.153|200\.200' /tmp/mtr_te_map.env 2>/dev/null || echo "  no map file"
pgrep -af te_rewrite_nfqueue || echo "  te_rewrite NOT running"
tail -3 /tmp/te_rewrite_nfqueue.log 2>/dev/null || true

echo
echo "========== 3) iptables mangle FORWARD (time-exceeded) =========="
iptables -t mangle -L FORWARD -n -v --line-numbers 2>/dev/null | grep -E 'time-exceeded|NFQUEUE|Chain' || true

echo
echo "========== 4) env interfaces (TE sync uses) =========="
echo "  DOWN=$DOWN UP=$UP MGMT=$MGMT"
for k in MTR_TE_REWRITE_OIF MTR_TE_REWRITE_IIF MTR_OP_DOWNSTREAM_IFACE MTR_BGP_RR_UPLINK_IFACE; do
  v=$(grep -E "^${k}=" /etc/mtr-op.env 2>/dev/null || grep -E "^${k}=" /opt/mtr-op/.env 2>/dev/null || true)
  [ -n "$v" ] && echo "  $v"
done

echo
echo "========== 5) route get 105.94 return (TE 走哪条出接口) =========="
ip route get 139.159.105.94 from 8.8.8.8 iif "$UP" 2>&1 | head -2

echo
echo "========== 6) 28s 抓 TE: src $MATCH or $FORGE (三接口) =========="
SEC=28
FILTER="icmp and icmp[icmptype]=11 and (src $MATCH or src $FORGE)"
for dev in "$UP" "$DOWN" "$MGMT"; do
  timeout "$SEC" tcpdump -ni "$dev" -c 20 -l -n "$FILTER" 2>/dev/null >"/tmp/te_${dev}.txt" &
done
wait 2>/dev/null || true
for dev in "$UP" "$DOWN" "$MGMT"; do
  n=$(wc -l <"/tmp/te_${dev}.txt" 2>/dev/null || echo 0)
  echo "  $dev: $n TE packets (src $MATCH or $FORGE)"
  head -3 "/tmp/te_${dev}.txt" 2>/dev/null | sed 's/^/    /'
done

echo
echo "========== 7) 结论提示 =========="
echo "  逐跳替换只改「转发的 ICMP Time Exceeded」外层源 IP，且须命中 mangle:"
echo "    -o $DOWN  或  -i $UP -o $DOWN"
echo "  若回程走 $MGMT，TE 不会进 NFQUEUE，规则不生效。"
