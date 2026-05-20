#!/bin/bash
# 208 mtr 8.8.8.8 联调抓包 + 诊断
set -uo pipefail
DST=8.8.8.8
PEER=139.159.43.208
RR=139.159.43.249
SELF=139.159.43.207
UPLINK="${MTR_BGP_RR_UPLINK_IFACE:-enp59s0f0np0}"
DOWN="${MTR_OP_DOWNSTREAM_IFACE:-eno1np0}"
SEC="${TCPDUMP_SEC:-35}"
FILTER="host $DST and (host $PEER or host $SELF or host $RR)"

echo "========== time $(date -Is) =========="
echo "uplink=$UPLINK down=$DOWN filter=$FILTER sec=$SEC"
echo

echo "========== 1) 路由 / 策略 =========="
ip route get "$DST" from "$PEER" iif "$DOWN" 2>&1 || true
ip route get "$PEER" from "$DST" iif "$UPLINK" 2>&1 || true
ip route show table 2110 2>/dev/null || true
ip -4 rule list | grep -E '^30:|^41:|2110' || true
echo

echo "========== 2) 转发计数（抓包前）=========="
iptables -L FORWARD -n -v 2>/dev/null | head -8
echo

echo "========== 3) 并行抓包 ${SEC}s（208 请保持 mtr）=========="
UP_LOG=/tmp/cap_up_88.txt
DN_LOG=/tmp/cap_dn_88.txt
: > "$UP_LOG"
: > "$DN_LOG"
(timeout "$SEC" tcpdump -ni "$UPLINK" -vv -c 120 "$FILTER" 2>&1 | tee -a "$UP_LOG") &
PID_UP=$!
(timeout "$SEC" tcpdump -ni "$DOWN" -vv -c 120 "$FILTER" 2>&1 | tee -a "$DN_LOG") &
PID_DN=$!
# 宽一点：凡涉及 8.8.8.8
(timeout "$SEC" tcpdump -ni "$DOWN" -c 40 "host $DST" 2>&1 | sed 's/^/[dn-any] /') &
(timeout "$SEC" tcpdump -ni "$UPLINK" -c 40 "host $DST" 2>&1 | sed 's/^/[up-any] /') &
wait $PID_UP $PID_DN 2>/dev/null || true
wait 2>/dev/null || true
echo

echo "========== 4) 抓包摘要 =========="
for f in "$DN_LOG" "$UP_LOG"; do
  echo "--- $f ---"
  n=$(grep -cE '^[0-9]{2}:' "$f" 2>/dev/null || echo 0)
  echo "lines=$n"
  grep -E "$PEER|$SELF" "$f" 2>/dev/null | head -15 || echo "(no 208/207 in narrow filter)"
  echo "last 5:"
  tail -5 "$f" 2>/dev/null || true
  echo
done

echo "========== 5) 转发计数（抓包后）=========="
iptables -L FORWARD -n -v 2>/dev/null | head -8
echo

echo "========== 6) RR / 邻居 =========="
ss -tn state established '( sport = :179 or dport = :179 )' 2>/dev/null | head -10 || true
curl -sf "http://127.0.0.1:9179/api/rib/routes?window=upstream&vrf=default&neighbor_ip=${RR}&prefix=0.0.0.0/0&page_size=3" 2>/dev/null | python3 -m json.tool 2>/dev/null | head -20 || true
curl -sf "http://127.0.0.1:9179/api/gobgp/status" 2>/dev/null | python3 -c "
import sys,json
try:
  d=json.load(sys.stdin)
  for s in (d.get('sessions') or d.get('neighbors') or []):
    if isinstance(s,dict) and '249' in str(s):
      print(s)
except Exception as e:
  print('status:', e)
" 2>/dev/null || true
echo

echo "========== 7) 本机试 ping（对照）=========="
ping -c 2 -W 1 "$DST" 2>&1 | tail -3 || true
ping -c 2 -W 1 -I "$PEER" "$DST" 2>&1 | tail -3 || true

echo "========== done $(date -Is) =========="
