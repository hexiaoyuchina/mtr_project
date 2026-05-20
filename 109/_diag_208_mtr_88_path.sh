#!/bin/bash
# 208 mtr 8.8.8.8 是否经 109 代答口走上联
set -euo pipefail
DST=8.8.8.8
UPLINK="${MTR_BGP_RR_UPLINK_IFACE:-enp59s0f0np0}"
DOWN="${MTR_OP_DOWNSTREAM_IFACE:-eno1np0}"
SPOOF=139.159.43.249
PEER208=139.159.43.208
SELF207=139.159.43.207

echo "========== 1) 接口 / ipvlan 代答 =========="
ip -br link show "$UPLINK" "$DOWN" iv249@eno1np0 2>/dev/null || ip -br link | grep -E 'enp59|eno1|iv249' || true
echo
ip -br addr show "$UPLINK" "$DOWN" 2>/dev/null || true
ip -br addr show dev iv249 2>/dev/null || echo "no iv249"
echo

echo "========== 2) 转发开关 =========="
sysctl -n net.ipv4.ip_forward 2>/dev/null || true
echo

echo "========== 3) 到 $DST 的路由（主表 / 卫星 VRF）=========="
ip route get "$DST" 2>/dev/null || true
ip route get "$DST" from "$PEER208" iif "$DOWN" 2>/dev/null || echo "route get from 208 iif down: fail"
ip route get "$DST" from "$SPOOF" 2>/dev/null || true
for vrf in default vbgp13915943249; do
  echo "--- vrf $vrf ---"
  ip vrf exec "$vrf" ip route get "$DST" 2>/dev/null || true
done
echo

echo "========== 4) 默认路由 / 208 相关 =========="
ip route show table main | grep -E 'default|0.0.0.0' || true
ip route show table main | grep -E '249|208|8\.8' || true
ip rule list 2>/dev/null | head -20
echo

echo "========== 5) ARP 引流 / OP 总开关 =========="
curl -sf http://127.0.0.1:8808/api/arp-spoof/settings 2>/dev/null | python3 -m json.tool 2>/dev/null | head -20 || echo "OP 8808 unreachable"
curl -sf http://127.0.0.1:8808/api/global 2>/dev/null | python3 -m json.tool 2>/dev/null || true
curl -sf 'http://127.0.0.1:8808/api/arp-spoof/targets' 2>/dev/null | python3 -c "
import sys,json
try:
  d=json.load(sys.stdin)
  for t in (d if isinstance(d,list) else d.get('items',d.get('targets',[]))):
    print(t)
except Exception as e:
  print('targets parse:', e)
" 2>/dev/null || true
echo

echo "========== 6) BGP 下游是否学到/通告默认或公网 =========="
curl -sf "http://127.0.0.1:9179/api/rib/routes?window=downstream&vrf=vbgp13915943249&neighbor_ip=${PEER208}&prefix=0.0.0.0/0&page=1&page_size=5" 2>/dev/null | python3 -m json.tool 2>/dev/null | head -30 || true
curl -sf "http://127.0.0.1:9179/api/rib/routes/count?window=downstream&vrf=vbgp13915943249&neighbor_ip=${PEER208}" 2>/dev/null || true
echo
curl -sf "http://127.0.0.1:9179/api/rib/routes?window=upstream&vrf=default&neighbor_ip=${SPOOF}&prefix=0.0.0.0/0&page=1&page_size=3" 2>/dev/null | python3 -m json.tool 2>/dev/null | head -25 || true
echo

echo "========== 7) nft / iptables 与 ICMP =========="
nft list table inet mtr_spoof 2>/dev/null | head -40 || echo "no mtr_spoof nft"
iptables -t mangle -L FORWARD -n -v 2>/dev/null | head -15 || true
pgrep -af 'mtr_spoof|te_rewrite' || echo "no nfqueue daemons"
echo

echo "========== 8) 并行抓包 15s（请同时在 208 上 mtr -n $DST）=========="
FILTER="host $DST"
(timeout 15 tcpdump -ni "$DOWN" -c 30 "$FILTER" 2>&1 | sed 's/^/[down] /') &
(timeout 15 tcpdump -ni "$UPLINK" -c 30 "$FILTER" 2>&1 | sed 's/^/[uplink] /') &
wait
echo

echo "========== 9) 本机 ping $DST =========="
ping -c 2 -W 2 "$DST" || true
