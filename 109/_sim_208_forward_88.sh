#!/bin/bash
set -e
DST=8.8.8.8
PEER=139.159.43.208
DOWN=eno1np0
UP=enp59s0f0np0
echo "=== simulate forward: ping -I $PEER (may need raw) ==="
# 用 ping 带源地址模拟 208 出站（经 table 2110 需从 down 入；本机发包走 OUTPUT 不一定同路径）
ping -c 2 -W 1 -I "$PEER" "$DST" 2>&1 || true
echo
echo "=== hping3 if present ==="
command -v hping3 >/dev/null && hping3 -1 "$DST" -a "$PEER" -c 2 -i u100000 2>&1 | tail -5 || echo "no hping3"
echo
echo "=== tcpdump 8s uplink+down while ping -I peer ==="
FILTER="host $DST"
(timeout 8 tcpdump -ni "$UP" -c 15 "$FILTER" 2>&1 | sed 's/^/[up] /') &
(timeout 8 tcpdump -ni "$DOWN" -c 15 "$FILTER" 2>&1 | sed 's/^/[dn] /') &
sleep 1
ping -c 3 -W 1 -I "$PEER" "$DST" >/tmp/p2.log 2>&1 || true
wait
cat /tmp/p2.log
