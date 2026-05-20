#!/bin/bash
DST=8.8.8.8
PEER=139.159.43.208
SRC=139.159.105.94
DOWN=eno1np0
UP=enp59s0f0np0
MGMT=enp59s0f1np1
SEC=28
FILTER="host $DST and (host $PEER or host $SRC)"

echo "========== $(date -Is) return path ${SEC}s =========="
echo "filter: $FILTER"
echo
echo "=== rule / table 2110 ==="
ip -4 rule list | grep -E '^30:' || true
ip route show table 2110
echo
echo "=== route get forward (iif down) ==="
ip route get "$DST" from "$PEER" iif "$DOWN" 2>&1 | head -2
ip route get "$DST" from "$SRC" iif "$DOWN" 2>&1 | head -2
echo "=== route get return (from $DST iif uplink) ==="
ip route get "$PEER" from "$DST" iif "$UP" 2>&1 | head -2
ip route get "$SRC" from "$DST" iif "$UP" 2>&1 | head -2
echo "=== main /32 on $DOWN ==="
ip route show table main | grep -E "$PEER|$SRC" || true
echo

capture() { timeout "$SEC" tcpdump -ni "$1" -l -n "$FILTER" 2>/dev/null >"/tmp/ret_$1.txt" & }
capture "$DOWN"; capture "$UP"; capture "$MGMT"
wait 2>/dev/null || true

summ() {
  local label=$1 dev=$2 f="/tmp/ret_$2.txt"
  echo "--- $label ($dev) ---"
  if [ ! -s "$f" ]; then echo "  (无)"; return; fi
  grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ > [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "$f" | sort | uniq -c | sort -rn
  echo -n "  req: "; grep -ci 'echo request' "$f"
  echo -n "  reply: "; grep -ci 'echo reply' "$f"
  echo -n "  TE: "; grep -ci 'time exceeded' "$f"
}

summ "下联" "$DOWN"
summ "上联" "$UP"
summ "管理" "$MGMT"

echo
echo "=== 回程统计 (8.8.8.8 -> 客户端) ==="
for pair in "$PEER:$PEER" "$SRC:$SRC"; do
  ip="${pair%%:*}"
  echo "--- to $ip ---"
  for dev in "$DOWN" "$UP" "$MGMT"; do
    n=$(grep -c "$DST > $ip" "/tmp/ret_$dev.txt" 2>/dev/null || echo 0)
    echo "  $dev: $n"
  done
done

echo
echo "=== 路径结论 ==="
R94_UP=$(grep -c "$DST > $SRC" "/tmp/ret_${UP}.txt" 2>/dev/null || echo 0)
R94_DN=$(grep -c "$DST > $SRC" "/tmp/ret_${DOWN}.txt" 2>/dev/null || echo 0)
R94_MG=$(grep -c "$DST > $SRC" "/tmp/ret_${MGMT}.txt" 2>/dev/null || echo 0)
R208_UP=$(grep -c "$DST > $PEER" "/tmp/ret_${UP}.txt" 2>/dev/null || echo 0)
R208_DN=$(grep -c "$DST > $PEER" "/tmp/ret_${DOWN}.txt" 2>/dev/null || echo 0)
echo "105.94 回程 Reply: 上联=$R94_UP 下联=$R94_DN 管理=$R94_MG"
echo "208   回程 Reply: 上联=$R208_UP 下联=$R208_DN"
if [ "$R94_DN" -gt 0 ] 2>/dev/null || [ "$R208_DN" -gt 0 ] 2>/dev/null; then
  echo "=> 回程已从下联转出"
elif [ "$R94_UP" -gt 0 ] 2>/dev/null || [ "$R208_UP" -gt 0 ] 2>/dev/null; then
  if [ "$R94_MG" -gt 0 ] 2>/dev/null; then
    echo "=> 回程上联有包；105.94 可能仍经管理口"
  else
    echo "=> 回程仅见上联，下联无（查主表 /32 或是否在 mtr）"
  fi
else
  echo "=> 未见回程 Reply（请确认 208 正在 mtr）"
fi
