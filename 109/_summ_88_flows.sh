#!/bin/bash
UP=enp59s0f0np0
DOWN=eno1np0
DST=8.8.8.8
SEC=15

echo "=== 上联 $UP ${SEC}s：凡涉及 $DST 的 IPv4 五元组(简化为 src>dst) ==="
timeout "$SEC" tcpdump -ni "$UP" -n -l "host $DST" 2>/dev/null | \
  grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ > [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | sort | uniq -c | sort -rn

echo
echo "=== 下联 $DOWN ${SEC}s：同上 ==="
timeout "$SEC" tcpdump -ni "$DOWN" -n -l "host $DST" 2>/dev/null | \
  grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ > [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | sort | uniq -c | sort -rn

echo
echo "=== 历史 cap_up_88（联调）src>dst 统计 ==="
grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ > [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' /tmp/cap_up_88.txt 2>/dev/null | sort | uniq -c | sort -rn

echo
echo "=== 历史 cap_dn_88（联调）src>dst 统计 ==="
grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ > [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' /tmp/cap_dn_88.txt 2>/dev/null | sort | uniq -c | sort -rn
