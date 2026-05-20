#!/bin/bash
set -uo pipefail
MATCH=148.153.127.105
FORGE=200.200.200.200
SRC=139.159.105.94
DST=8.8.8.8
DOWN=eno1np0
UP=enp59s0f0np0
MGMT=enp59s0f1np1
SEC=20

echo "========== 1) hop 规则 / TE map / 进程 =========="
curl -sf http://127.0.0.1:8808/api/hop-rules 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    if not d: print('  (无规则)')
    for r in d:
        print(f\"  id={r.get('id')} en={r.get('enabled')} pri={r.get('priority')} {r.get('match_cidr')} -> {r.get('forged_src')}\")
except Exception as e:
    print('  API err', e)
" || curl -sf http://127.0.0.1:9179/api/hop-rules 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
for r in d: print(f\"  id={r.get('id')} en={r.get('enabled')} {r.get('match_cidr')} -> {r.get('forged_src')}\")
" || echo "  hop-rules API 不可用"

grep MTR_TE_REWRITE_MAP /tmp/mtr_te_map.env 2>/dev/null || echo "  无 mtr_te_map.env"
pgrep -af te_rewrite_nfqueue || echo "  te_rewrite 未运行"
tail -2 /tmp/te_rewrite_nfqueue.log 2>/dev/null || true

echo
echo "========== 2) iptables NFQUEUE（仅 time-exceeded）=========="
iptables -t mangle -L FORWARD -n -v --line-numbers 2>/dev/null | grep -E 'Chain|time-exceeded|NFQUEUE' || true

echo
echo "========== 3) 105.94 回程选路（决定 TE 是否走下联 NFQUEUE）=========="
ip route get "$SRC" from "$DST" iif "$UP" 2>&1 | head -2
ip route get "$SRC" 2>&1 | head -2

echo
echo "========== 4) ${SEC}s 抓 TE src=$MATCH 或 $FORGE（请在 208 mtr -a $SRC $DST）=========="
F="icmp and icmp[icmptype]=11 and (src $MATCH or src $FORGE)"
for label dev in 上联:$UP 下联:$DOWN 管理:$MGMT; do
  name=${label%%:*}; iface=${label##*:}
  timeout "$SEC" tcpdump -ni "$iface" -c 15 -l -n "$F" 2>/dev/null >"/tmp/hr_$iface.txt" || true
  n=$(wc -l <"/tmp/hr_$iface.txt" 2>/dev/null || echo 0)
  echo "  $name($iface): $n 条"
  head -2 "/tmp/hr_$iface.txt" 2>/dev/null | sed 's/^/    /'
done

echo
echo "========== 5) 结论 =========="
UP_M=$(grep -c "src $MATCH" "/tmp/hr_${UP}.txt" 2>/dev/null || echo 0)
DN_M=$(grep -c "src $MATCH" "/tmp/hr_${DOWN}.txt" 2>/dev/null || echo 0)
MG_M=$(grep -c "src $MATCH" "/tmp/hr_${MGMT}.txt" 2>/dev/null || echo 0)
FG=$(grep -c "src $FORGE" "/tmp/hr_${UP}.txt" "/tmp/hr_${DOWN}.txt" "/tmp/hr_${MGMT}.txt" 2>/dev/null | awk -F: '{s+=$2} END{print s+0}')
if [ "$FG" -gt 0 ] 2>/dev/null; then
  echo "  已见到改写后源 $FORGE"
elif [ "$UP_M" -gt 0 ] || [ "$MG_M" -gt 0 ]; then
  echo "  TE 仍为 $MATCH；上联/管理有包、下联少 → 未进 -o $DOWN 的 NFQUEUE，改写不生效"
else
  echo "  未抓到 $MATCH TE（请确认正在 mtr）"
fi
