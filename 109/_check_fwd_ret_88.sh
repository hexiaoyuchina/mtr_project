#!/bin/bash
# 仅核查：1) 208->8.8.8.8 下联进、上联出  2) 是否有回程
PEER=139.159.43.208
DST=8.8.8.8
DOWN=eno1np0
UP=enp59s0f0np0
SEC=20

echo "=== $(date -Is) 抓包 ${SEC}s（请保持 208 mtr $DST）==="

DN_FWD=/tmp/chk_dn_fwd.txt
UP_FWD=/tmp/chk_up_fwd.txt
DN_RET=/tmp/chk_dn_ret.txt
UP_RET=/tmp/chk_up_ret.txt
: >"$DN_FWD"; : >"$UP_FWD"; : >"$DN_RET"; : >"$UP_RET"

# 去程：208 -> 8.8.8.8
(timeout "$SEC" tcpdump -ni "$DOWN" -l -n "$PEER > $DST" 2>/dev/null | tee -a "$DN_FWD") &
(timeout "$SEC" tcpdump -ni "$UP" -l -n "$PEER > $DST" 2>/dev/null | tee -a "$UP_FWD") &
# 回程：8.8.8.8 -> 208 或 ICMP TE/Reply
(timeout "$SEC" tcpdump -ni "$DOWN" -l -n "$DST > $PEER" 2>/dev/null | tee -a "$DN_RET") &
(timeout "$SEC" tcpdump -ni "$UP" -l -n "$DST > $PEER" 2>/dev/null | tee -a "$UP_RET") &
wait 2>/dev/null || true

cnt() { wc -l <"$1" 2>/dev/null | tr -d ' '; }

echo
echo "========== 1) 去程 208 -> $DST =========="
echo "下联 $DOWN: $(cnt "$DN_FWD") 行"
echo "上联 $UP:   $(cnt "$UP_FWD") 行"
if [ "$(cnt "$DN_FWD")" -gt 0 ]; then
  echo "下联样例(前3):"
  head -3 "$DN_FWD"
fi
if [ "$(cnt "$UP_FWD")" -gt 0 ]; then
  echo "上联样例(前3):"
  head -3 "$UP_FWD"
else
  echo "上联样例: (无)"
fi
# TTL 分布（去程）
echo "下联 TTL 分布:"
grep -oE 'ttl [0-9]+' "$DN_FWD" 2>/dev/null | sort | uniq -c | sort -rn | head -8 || echo "  (无)"
echo "上联 TTL 分布:"
grep -oE 'ttl [0-9]+' "$UP_FWD" 2>/dev/null | sort | uniq -c | sort -rn | head -8 || echo "  (无)"

echo
echo "========== 2) 回程 $DST -> 208 =========="
echo "下联 $DOWN: $(cnt "$DN_RET") 行"
echo "上联 $UP:   $(cnt "$UP_RET") 行"
if [ "$(cnt "$DN_RET")" -gt 0 ]; then
  echo "下联回程样例:"
  head -5 "$DN_RET"
fi
if [ "$(cnt "$UP_RET")" -gt 0 ]; then
  echo "上联回程样例:"
  head -5 "$UP_RET"
else
  echo "上联/下联回程: 均无匹配包"
fi

# 任意涉及 8.8.8.8 的 icmp 非 208->dst（看是否有别的回程形态）
echo
echo "========== 3) 补充：下联任意 $DST 相关 ICMP（20s 计数）=========="
timeout "$SEC" tcpdump -ni "$DOWN" -n -c 200 "host $DST and icmp" 2>&1 | \
  awk '/ICMP/ {
    if ($0 ~ />/) { split($3,a,"."); split($5,b,"."); }
  }
  END { }
  ' >/tmp/chk_dn_any.txt 2>&1
# 简单统计
timeout 8 tcpdump -ni "$DOWN" -n -c 100 "host $DST and icmp" 2>/dev/null | \
  grep -cE 'ICMP echo reply|time exceeded' || echo "0 reply/te on down (8s sample)"
timeout 8 tcpdump -ni "$UP" -n -c 100 "host $DST and icmp" 2>/dev/null | \
  grep -cE 'ICMP echo reply|time exceeded' || echo "0 reply/te on up (8s sample)"

echo
echo "=== done ==="
