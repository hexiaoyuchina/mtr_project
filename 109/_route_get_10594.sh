#!/bin/bash
DST=8.8.8.8
DOWN=eno1np0
UP=enp59s0f0np0
MGMT=enp59s0f1np1

echo "=== 本机地址 ==="
ip -br addr show "$DOWN" "$UP" "$MGMT" 2>/dev/null
echo

echo "=== ip rule（与 2110 相关）==="
ip -4 rule list | grep -E '^30:|^41:|2103|2110' || true
echo

echo "=== route get：从不同源、不同 iif ==="
for src in 139.159.43.208 139.159.105.94 139.159.43.207; do
  echo "--- from $src iif $DOWN ---"
  ip route get "$DST" from "$src" iif "$DOWN" 2>&1 | head -2
done
echo "--- from 139.159.105.94（无 iif，本机发包）---"
ip route get "$DST" from 139.159.105.94 2>&1 | head -2
echo "--- 本机默认 ---"
ip route get "$DST" 2>&1 | head -2
echo

echo "=== table 2110 ==="
ip route show table 2110
echo

echo "=== main 里 8.8.8.8 / default ==="
ip route show table main | grep -E 'default|8\.8\.8\.8' | head -5
