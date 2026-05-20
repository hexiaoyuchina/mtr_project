#!/bin/bash
SRC=139.159.105.94
DST=8.8.8.8
MGMT_IP=101.89.68.109
DOWN=eno1np0
UP=enp59s0f0np0
MGMT=enp59s0f1np1
SEC=25
FILTER="(host $SRC or host $DST or host $MGMT_IP or host 148.153.127.105 or host 139.159.43.249) and icmp"

echo "========== $(date -Is) ${SEC}s =========="
ip route get "$DST" from "$SRC" iif "$DOWN" 2>&1 | head -1
ip route get "$SRC" from "$DST" iif "$UP" 2>&1 | head -1
echo

for iface in "$DOWN" "$UP" "$MGMT"; do
  timeout "$SEC" tcpdump -ni "$iface" -l -n "$FILTER" 2>/dev/null >"/tmp/flow_${iface}.txt" &
done
wait 2>/dev/null || true

for iface in "$DOWN" "$UP" "$MGMT"; do
  echo "=== $iface ==="
  if [ ! -s "/tmp/flow_${iface}.txt" ]; then echo "  empty"; continue; fi
  grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ > [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "/tmp/flow_${iface}.txt" | sort | uniq -c | sort -rn | head -8
  echo -n "  req "; grep -c "${SRC} > ${DST}" "/tmp/flow_${iface}.txt" 2>/dev/null || echo 0
  echo -n "  rep "; grep -ci 'echo reply' "/tmp/flow_${iface}.txt" 2>/dev/null || echo 0
  echo -n "  te  "; grep -ci 'time exceeded' "/tmp/flow_${iface}.txt" 2>/dev/null || echo 0
done

echo
echo "=== forward ${SRC}>${DST} per iface ==="
for iface in "$DOWN" "$UP" "$MGMT"; do
  n=$(grep -c "$SRC > $DST" "/tmp/flow_${iface}.txt" 2>/dev/null || echo 0)
  echo "  $iface: $n"
done
echo "=== return TE 148>${SRC} per iface ==="
for iface in "$DOWN" "$UP" "$MGMT"; do
  n=$(grep -c "148.153.127.105 > $SRC" "/tmp/flow_${iface}.txt" 2>/dev/null || echo 0)
  echo "  $iface: $n"
done
echo "=== return TE ${MGMT_IP}>${SRC} per iface ==="
for iface in "$DOWN" "$UP" "$MGMT"; do
  n=$(grep -c "$MGMT_IP > $SRC" "/tmp/flow_${iface}.txt" 2>/dev/null || echo 0)
  echo "  $iface: $n"
done
echo "=== return Reply ${DST}>${SRC} per iface ==="
for iface in "$DOWN" "$UP" "$MGMT"; do
  n=$(grep -c "$DST > $SRC" "/tmp/flow_${iface}.txt" 2>/dev/null || echo 0)
  echo "  $iface: $n"
done
