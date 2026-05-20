#!/bin/bash
set -uo pipefail
SRC=139.159.105.94
DST=8.8.8.8
MGMT_IP=101.89.68.109
DOWN=eno1np0
UP=enp59s0f0np0
MGMT=enp59s0f1np1
SEC=30
FILTER="(host $SRC or host $DST or host $MGMT_IP or host 148.153.127.105 or host 139.159.43.249) and icmp"

echo "========== $(date -Is) capture ${SEC}s (mtr -a $SRC $DST) =========="
echo "=== routing ==="
ip route show table 2111
ip -4 rule list | grep -E '^29:|^30:|^31:' || true
ip route get "$DST" from "$SRC" iif "$DOWN" 2>&1 | head -2
ip route get "$SRC" from "$DST" iif "$UP" 2>&1 | head -2
ip route get "$SRC" 2>&1 | head -2
echo "=== neigh eno1np0 (105.94 / 208) ==="
ip neigh show dev "$DOWN" | grep -E '105\.94|43\.208' || echo "(none)"
echo

for iface in "$DOWN" "$UP" "$MGMT"; do
  timeout "$SEC" tcpdump -ni "$iface" -l -n "$FILTER" 2>/dev/null >"/tmp/cap2111_${iface}.txt" &
done
wait 2>/dev/null || true

for iface in "$DOWN" "$UP" "$MGMT"; do
  echo "=== $iface ==="
  if [ ! -s "/tmp/cap2111_${iface}.txt" ]; then echo "  empty"; continue; fi
  grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ > [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "/tmp/cap2111_${iface}.txt" | sort | uniq -c | sort -rn | head -10
  echo -n "  req ${SRC}>${DST}: "; grep -c "$SRC > $DST" "/tmp/cap2111_${iface}.txt" 2>/dev/null || echo 0
  echo -n "  rep ${DST}>${SRC}: "; grep -ci "echo reply" "/tmp/cap2111_${iface}.txt" 2>/dev/null || echo 0
  echo -n "  te to ${SRC}: "; grep -ci 'time exceeded' "/tmp/cap2111_${iface}.txt" 2>/dev/null || echo 0
  echo -n "  te ${MGMT_IP}>${SRC}: "; grep -c "$MGMT_IP > $SRC" "/tmp/cap2111_${iface}.txt" 2>/dev/null || echo 0
  echo -n "  te 148>${SRC}: "; grep -c "148.153.127.105 > $SRC" "/tmp/cap2111_${iface}.txt" 2>/dev/null || echo 0
done

echo
echo "=== TE samples down (to $SRC) ==="
grep 'time exceeded' "/tmp/cap2111_${DOWN}.txt" 2>/dev/null | head -5 | sed 's/^/  /' || echo "  (none)"
echo "=== TE samples mgmt ==="
grep 'time exceeded' "/tmp/cap2111_${MGMT}.txt" 2>/dev/null | head -3 | sed 's/^/  /' || echo "  (none)"
