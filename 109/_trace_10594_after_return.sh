#!/bin/bash
SRC=139.159.105.94
DST=8.8.8.8
DOWN=eno1np0
UP=enp59s0f0np0
MGMT=enp59s0f1np1
SEC=25

echo "========== $(date -Is) src=$SRC mtr $DST ${SEC}s =========="
echo "=== route get return ==="
ip route get "$SRC" from "$DST" iif "$UP" 2>&1
ip route show table main | grep -E "$SRC|139.159.43.208" | grep "$DOWN" || true
echo

capture() { timeout "$SEC" tcpdump -ni "$1" -l -n "host $SRC and host $DST" 2>/dev/null >"/tmp/tr2_$1.txt" & }

capture "$DOWN"
capture "$UP"
capture "$MGMT"
wait 2>/dev/null || true

summ() {
  local name=$1 f="/tmp/tr2_$2.txt"
  echo "--- $name ---"
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
echo "=== 路径结论 ==="
DN_R=$(grep -c "$DST > $SRC" "/tmp/tr2_${DOWN}.txt" 2>/dev/null || echo 0)
UP_R=$(grep -c "$DST > $SRC" "/tmp/tr2_${UP}.txt" 2>/dev/null || echo 0)
MG_R=$(grep -c "$DST > $SRC" "/tmp/tr2_${MGMT}.txt" 2>/dev/null || echo 0)
DN_F=$(grep -ci 'echo request' "/tmp/tr2_${DOWN}.txt" 2>/dev/null || echo 0)
UP_F=$(grep -ci 'echo request' "/tmp/tr2_${UP}.txt" 2>/dev/null || echo 0)
echo "去程 req: 下联=$DN_F 上联=$UP_F"
echo "回程 $DST>$SRC: 下联=$DN_R 上联=$UP_R 管理=$MG_R"
if [ "$DN_R" -gt 0 ] 2>/dev/null; then
  echo "回程已从下联回到客户端方向"
else
  echo "下联仍无回程(请确认 208 正在 mtr -a $SRC)"
fi
