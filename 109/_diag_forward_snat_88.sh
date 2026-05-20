#!/bin/bash
DST=8.8.8.8
PEER=139.159.43.208
RR=139.159.43.249
SELF=139.159.43.207
UP=enp59s0f0np0
DOWN=eno1np0

echo "========== NAT / POSTROUTING =========="
iptables -t nat -L -n -v 2>/dev/null | head -30
nft list table ip mtr_te_snat 2>/dev/null | head -20
echo

echo "========== 上联：207 源 / TE / 回程 15s =========="
timeout 15 tcpdump -ni "$UP" -vv -c 50 \
  '(host '"$DST"' and (host '"$SELF"' or host '"$PEER"' or icmp[icmptype]=11 or icmp[icmptype]=0))' 2>&1 | head -40
echo

echo "========== 下联：TE / reply 15s =========="
timeout 15 tcpdump -ni "$DOWN" -vv -c 30 \
  'host '"$DST"' and (icmp[icmptype]=11 or icmp[icmptype]=0 or host '"$SELF"')' 2>&1 | head -25
echo

echo "========== RR 学到的路由抽样 =========="
curl -sf "http://127.0.0.1:9179/api/rib/routes?window=upstream&vrf=default&neighbor_ip=${RR}&page=1&page_size=15" 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('total', d.get('total'))
for r in (d.get('routes') or [])[:15]:
  print(r.get('prefix'), 'nh', r.get('nexthop'))
" 2>/dev/null || true
curl -sf "http://127.0.0.1:9179/api/rib/routes?window=upstream&vrf=default&neighbor_ip=${RR}&prefix=8.8.8.8/32&page_size=5" 2>/dev/null | python3 -m json.tool 2>/dev/null | head -20
echo

echo "========== 主表到 8.8.8.8 / 249 =========="
ip route get "$DST" 2>&1
ip route get "$RR" 2>&1
arp -n "$RR" 2>/dev/null | head -3
