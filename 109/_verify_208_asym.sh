#!/bin/bash
PEER=139.159.43.208
DST=8.8.8.8
DOWN=eno1np0
UP=enp59s0f0np0
MGMT=enp59s0f1np1

echo "========== 1) 去程/回程 route get（源 208）=========="
echo "--- 去程：from $PEER iif $DOWN ---"
ip route get "$DST" from "$PEER" iif "$DOWN" 2>&1
echo "--- 回程：to $PEER from $DST iif $UP ---"
ip route get "$PEER" from "$DST" iif "$UP" 2>&1
echo "--- 回程：to $PEER（无 iif）---"
ip route get "$PEER" 2>&1
echo

echo "========== 2) main / 2110 中与 208、43 网段相关 =========="
ip route show table main | grep -E '208|43\.|default|101\.89' || true
ip route show table 2110
echo

echo "========== 3) 主表是否把 208 指到上联（错误路径）=========="
ip route get "$PEER" from "$DST" iif "$UP" 2>&1
# 若 208 被 43.0/24 判到 uplink，这里 oif 可能是 UP 而非 DOWN

echo
echo "========== 4) 抓包 20s：208<->88 在四口的去向（请保持 208 mtr -a 208）=========="
SEC=20
for iface in "$DOWN" "$UP" "$MGMT"; do
  echo "--- $iface ---"
  timeout "$SEC" tcpdump -ni "$iface" -n -l "host $PEER and host $DST" 2>/dev/null | \
    awk '
      / > / {
        split($0,a," > ");
        gsub(/:.*/,"",a[2]);
        n=split($1,b,".");
        src=b[1]"."b[2]"."b[3]"."b[4];
        dst=a[2];
        key=src" > "dst;
        c[key]++;
      }
      END { for (k in c) print c[k], k }
    ' | sort -rn
done

echo
echo "========== 5) 回程仅 88->208（上联进、应从下联出）=========="
timeout "$SEC" tcpdump -ni "$UP" -n -c 15 "$DST > $PEER" 2>&1 | head -8
echo "--- 同时看下联是否出现 88>208 ---"
timeout "$SEC" tcpdump -ni "$DOWN" -n -c 15 "$DST > $PEER" 2>&1 | head -8

echo
echo "========== 6) FORWARD 计数差值（20s 前后）=========="
iptables -L FORWARD -n -v -x | head -6
