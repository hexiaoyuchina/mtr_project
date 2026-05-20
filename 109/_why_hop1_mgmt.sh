#!/bin/bash
MGMT=enp59s0f1np1
DOWN=eno1np0
UP=enp59s0f0np0
PEER=139.159.43.208
SRC=139.159.105.94
DST=8.8.8.8

echo "=== 本机地址 ==="
ip -br addr show "$MGMT" "$DOWN" "$UP" 2>/dev/null
echo
echo "=== 主表 default ==="
ip route show table main | grep -E 'default|101\.89|43\.'
echo
echo "=== 从 109 发往客户端/公网 的源地址选路 ==="
ip route get "$PEER" 2>&1 | head -2
ip route get "$SRC" 2>&1 | head -2
ip route get "$DST" from "$SRC" iif "$DOWN" 2>&1 | head -2
echo
echo "=== 若本机对 TTL 过期发 TE：到 $SRC / $PEER 用何源 ==="
# 模拟「需回复给下游源」的选路（无 iif 时本机发包）
ip route get "$SRC" from "$DST" 2>&1 | head -2
ip route get "$PEER" from "$DST" 2>&1 | head -2
echo
echo "=== rule 30 / 31 ==="
ip -4 rule list | grep -E '^30:|^31:' || true
echo
echo "=== 105.94 回程（上联进）==="
ip route get "$SRC" from "$DST" iif "$UP" 2>&1 | head -2
