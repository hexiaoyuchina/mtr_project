#!/bin/bash
# 只读：客户端源 139.159.105.94 → 8.8.8.8 的 MTR/ICMP 抓包与路径快照（不改配置）
set +e
CLIENT=139.159.105.94
DST=8.8.8.8
DUR=45
CAP=/tmp/mtr_cap_$$.pcap
DOWN=eno1np0
UP=enp59s0f0np0

echo "=== 时间 $(date -Is) ==="
echo "CLIENT=$CLIENT DST=$DST DUR=${DUR}s CAP=$CAP"

echo ""
echo "=== 接口地址 ==="
ip -br addr show "$DOWN" "$UP" 2>/dev/null
ip -br addr show | grep -E 'iv249|209|207' || true

echo ""
echo "=== ip rule (2110 / client) ==="
ip rule list | grep -E '2110|105\.94|from 139\.159' || ip rule list | head -20

echo ""
echo "=== table 2110 ==="
ip route show table 2110 2>/dev/null | head -25

echo ""
echo "=== 到 8.8.8.8 / 从 CLIENT 查路 ==="
ip route get "$DST" from "$CLIENT" iif "$DOWN" 2>&1 | head -3
ip route get "$CLIENT" 2>&1 | head -3

echo ""
echo "=== TE / NFQUEUE 进程 ==="
pgrep -af 'te_rewrite|mtr_spoof|nfqueue' | head -8

echo ""
echo "=== mangle FORWARD (TE) ==="
iptables -t mangle -S FORWARD 2>/dev/null | grep -E 'NFQUEUE|eno1np0|enp59s0f0' | head -15

echo ""
echo "=== 开始抓包 ${DUR}s（请在客户端同时跑 MTR/Traceroute）==="
echo "filter: icmp and (host $CLIENT or host $DST)"
timeout "$DUR" tcpdump -i any -nn -s 256 -c 500 \
  "icmp and (host $CLIENT or host $DST)" \
  -w "$CAP" 2>/dev/null &
TP=$!
sleep "$DUR"
wait "$TP" 2>/dev/null

echo ""
echo "=== tcpdump 文本摘要 ==="
if [ -f "$CAP" ]; then
  tcpdump -nn -r "$CAP" 2>/dev/null | head -80
  echo "--- counts ---"
  tcpdump -nn -r "$CAP" 2>/dev/null | awk '
    {print $3,$5,$1}
  ' | sed 's/,$//' | sort | uniq -c | sort -rn | head -25
  ls -la "$CAP"
else
  echo "no pcap"
fi

echo ""
echo "=== 最近内核/TE 日志 ==="
journalctl -u mtr-op --since '3 min ago' --no-pager 2>/dev/null | tail -5
tail -20 /tmp/te_rewrite_nfqueue.log 2>/dev/null || tail -20 /tmp/mtr_spoof_nfqueue.log 2>/dev/null || echo "(no nfqueue log)"
