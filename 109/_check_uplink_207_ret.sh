#!/bin/bash
# 上联 207 口：去程/回程（208 mtr 8.8.8.8 相关）
UP=enp59s0f0np0
PEER=139.159.43.208
SELF=139.159.43.207
RR=139.159.43.249
DST=8.8.8.8
SEC=20

echo "=== 上联 $UP（本机 207/24）历史 cap_up_88.txt ==="
if [ -f /tmp/cap_up_88.txt ]; then
  echo -n "208>88: "; grep -c "$PEER > $DST\|$PEER.*>.*$DST" /tmp/cap_up_88.txt || echo 0
  echo -n "88>208: "; grep -c "$DST > $PEER\|$DST.*>.*$PEER" /tmp/cap_up_88.txt || echo 0
  echo -n "88>207: "; grep -c "$DST > $SELF\|$DST.*>.*$SELF" /tmp/cap_up_88.txt || echo 0
  echo -n "207>88: "; grep -c "$SELF > $DST\|$SELF.*>.*$DST" /tmp/cap_up_88.txt || echo 0
  echo -n "249>88: "; grep -c "$RR > $DST" /tmp/cap_up_88.txt || echo 0
  echo -n "88>249: "; grep -c "$DST > $RR" /tmp/cap_up_88.txt || echo 0
  echo -n "time exceeded: "; grep -ci "time exceeded" /tmp/cap_up_88.txt || echo 0
  echo -n "echo reply: "; grep -ci "echo reply" /tmp/cap_up_88.txt || echo 0
else
  echo "(无历史文件)"
fi

echo
echo "=== 现抓 ${SEC}s 上联（host 8.8.8.8）==="
OUT=/tmp/up_88_all.txt
timeout "$SEC" tcpdump -ni "$UP" -n -c 80 "host $DST" 2>&1 | tee "$OUT"
echo
echo "--- 分类 ---"
echo -n "208>88: "; grep -c "$PEER > $DST" "$OUT" 2>/dev/null || echo 0
echo -n "88>208: "; grep -c "$DST > $PEER" "$OUT" 2>/dev/null || echo 0
echo -n "88>207: "; grep -c "$DST > $SELF" "$OUT" 2>/dev/null || echo 0
echo -n "207>88: "; grep -c "$SELF > $DST" "$OUT" 2>/dev/null || echo 0
echo -n "TE/reply: "; grep -ciE 'time exceeded|echo reply' "$OUT" 2>/dev/null || echo 0
