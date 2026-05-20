#!/bin/bash
PEER=139.159.43.208
DST=8.8.8.8
DOWN=eno1np0
UP=enp59s0f0np0
MGMT=enp59s0f1np1
SEC=25

echo "========== $(date -Is) 208 mtr $DST 路径追踪 ${SEC}s =========="
echo
echo "=== 策略 / 选路 ==="
ip -4 rule list | grep -E '^30:|^41:|2110|2103' || true
ip route show table 2110
echo "--- route get ---"
ip route get "$DST" from "$PEER" iif "$DOWN" 2>&1
ip route get "$PEER" from "$DST" iif "$UP" 2>&1
echo

echo "=== 并行抓包 ${SEC}s ==="
DN=/tmp/trace_dn.txt
UPF=/tmp/trace_up.txt
MG=/tmp/trace_mgmt.txt
: >"$DN"; : >"$UPF"; : >"$MG"
(timeout "$SEC" tcpdump -ni "$DOWN" -l -n "host $PEER and host $DST" 2>/dev/null | tee -a "$DN") &
(timeout "$SEC" tcpdump -ni "$UP" -l -n "host $PEER and host $DST" 2>/dev/null | tee -a "$UPF") &
(timeout "$SEC" tcpdump -ni "$MGMT" -l -n "host $PEER and host $DST" 2>/dev/null | tee -a "$MG") &
wait 2>/dev/null || true

summ() {
  local f=$1 label=$2
  echo "--- $label ---"
  if [ ! -s "$f" ]; then echo "  (无包)"; return; fi
  grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ > [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "$f" 2>/dev/null | sort | uniq -c | sort -rn
  echo -n "  TE/reply: "; grep -ciE 'time exceeded|echo reply' "$f" || echo 0
  echo -n "  echo req: "; grep -ci 'echo request' "$f" || echo 0
  echo "  样例:"
  grep -E 'ICMP|> '"$DST" "$f" 2>/dev/null | head -4 | sed 's/^/    /'
}

summ "$DN" "下联 $DOWN"
summ "$UPF" "上联 $UP"
summ "$MG" "管理 $MGMT"

echo
echo "=== 上联 L2 样例（去程 1 包）==="
timeout 8 tcpdump -ni "$UP" -ee -c 2 "host $PEER and host $DST and icmp" 2>&1 | grep -E 'ICMP|0c:42|00:50' | head -4

echo
echo "=== 路径结论 ==="
DN_F=$(grep -c 'echo request' "$DN" 2>/dev/null || echo 0)
UP_F=$(grep -c 'echo request' "$UPF" 2>/dev/null || echo 0)
MG_F=$(grep -c 'echo request' "$MG" 2>/dev/null || echo 0)
DN_R=$(grep -ciE 'echo reply|time exceeded' "$DN" 2>/dev/null || echo 0)
UP_R=$(grep -ciE 'echo reply|time exceeded' "$UPF" 2>/dev/null || echo 0)

if [ "$DN_F" -gt 0 ] && [ "$UP_F" -gt 0 ]; then
  echo "去程: 208 -> $DST  下联进(${DN_F} req) -> 上联出(${UP_F} req)"
elif [ "$DN_F" -gt 0 ] && [ "$MG_F" -gt 0 ]; then
  echo "去程: 下联进 + 管理口也有(异常)"
elif [ "$DN_F" -eq 0 ]; then
  echo "去程: 未抓到 208->$DST (请确认 208 正在 mtr)"
else
  echo "去程: 仅下联 ${DN_F} req, 上联 ${UP_F} req"
fi

if [ "$DN_R" -gt 0 ] || [ "$UP_R" -gt 0 ]; then
  echo "回程: 下联 TE/reply=${DN_R} 上联 TE/reply=${UP_R}"
else
  echo "回程: 109 上未抓到 $DST -> $PEER 的 TE/Reply"
fi
