#!/bin/bash
PEER=139.159.43.208
DST=8.8.8.8
DOWN=eno1np0
UP=enp59s0f0np0
SEC=25

echo "=== A) 历史抓包文件（若存在）==="
for f in /tmp/cap_dn_88.txt /tmp/cap_up_88.txt /tmp/chk_dn_fwd.txt; do
  [ -f "$f" ] && echo "$f: $(wc -l <"$f") lines, 208->88: $(grep -c "$PEER.*$DST\|$PEER > $DST" "$f" 2>/dev/null || echo 0)" || true
done
[ -f /tmp/cap_dn_88.txt ] && echo "cap_dn 回程 88->208: $(grep -c "$DST.*$PEER\|$DST > $PEER" /tmp/cap_dn_88.txt 2>/dev/null || echo 0)"
[ -f /tmp/cap_up_88.txt ] && echo "cap_up 回程 88->208: $(grep -c "$DST.*$PEER\|$DST > $PEER" /tmp/cap_up_88.txt 2>/dev/null || echo 0)"

echo
echo "=== B) 现抓 ${SEC}s host $PEER and host $DST ==="
timeout "$SEC" tcpdump -ni "$DOWN" -n -c 60 "host $PEER and host $DST" 2>&1 | tee /tmp/live_dn_both.txt | wc -l
echo "--- uplink same filter ---"
timeout "$SEC" tcpdump -ni "$UP" -n -c 60 "host $PEER and host $DST" 2>&1 | tee /tmp/live_up_both.txt | wc -l

echo
echo "=== C) 去程/回程分类（现抓文件）==="
echo -n "下联 208>88: "; grep -c " $PEER > $DST" /tmp/live_dn_both.txt 2>/dev/null || echo 0
echo -n "下联 88>208: "; grep -c " $DST > $PEER" /tmp/live_dn_both.txt 2>/dev/null || echo 0
echo -n "上联 208>88: "; grep -c " $PEER > $DST" /tmp/live_up_both.txt 2>/dev/null || echo 0
echo -n "上联 88>208: "; grep -c " $DST > $PEER" /tmp/live_up_both.txt 2>/dev/null || echo 0
echo "下联样例:"
grep -E "$PEER > $DST|$DST > $PEER" /tmp/live_dn_both.txt 2>/dev/null | head -6
echo "上联样例:"
grep -E "$PEER > $DST|$DST > $PEER" /tmp/live_up_both.txt 2>/dev/null | head -6
