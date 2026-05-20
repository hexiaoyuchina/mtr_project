#!/bin/bash
SRC=139.159.105.94
DST=8.8.8.8
RR=139.159.43.249
SELF=139.159.43.207
DOWN=eno1np0
UP=enp59s0f0np0
MGMT=enp59s0f1np1
SEC=30

echo "========== $(date -Is) mtr $DST src=$SRC ${SEC}s =========="
echo
echo "=== routing ==="
ip -4 rule list | grep -E '^30:|^41:' || true
echo "--- from $SRC iif $DOWN ---"
ip route get "$DST" from "$SRC" iif "$DOWN" 2>&1
echo "--- return to $SRC from $DST iif $UP ---"
ip route get "$SRC" from "$DST" iif "$UP" 2>&1
echo

capture() {
  local dev=$1
  timeout "$SEC" tcpdump -ni "$dev" -l -n "host $SRC and host $DST" 2>/dev/null >"/tmp/tr_${dev}.txt" &
}

capture "$DOWN"
capture "$UP"
capture "$MGMT"
wait 2>/dev/null || true

summarize() {
  local name=$1 dev=$2
  local f="/tmp/tr_${dev}.txt"
  echo
  echo "--- $name ($dev) ---"
  if [ ! -s "$f" ]; then
    echo "  (no packets)"
    return
  fi
  grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ > [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "$f" | sort | uniq -c | sort -rn
  echo -n "  echo request: "; grep -ci 'echo request' "$f" || echo 0
  echo -n "  echo reply:   "; grep -ci 'echo reply' "$f" || echo 0
  echo -n "  time exceeded:"; grep -ci 'time exceeded' "$f" || echo 0
  echo "  samples:"
  grep ICMP "$f" | head -6 | sed 's/^/    /'
}

summarize "down" "$DOWN"
summarize "up" "$UP"
summarize "mgmt" "$MGMT"

echo
echo "=== uplink: return to $SRC or TE from 109/249 ==="
timeout 15 tcpdump -ni "$UP" -n -c 35 "host $DST and (host $SRC or host $RR or host $SELF or host 101.89.68.109)" 2>&1 | grep ICMP | head -20

echo
echo "=== down: return toward 105.x ==="
timeout 12 tcpdump -ni "$DOWN" -n -c 20 "host $DST and dst net 139.159.105.0/24" 2>&1 | grep ICMP | head -12

echo
echo "=== L2 uplink forward ==="
timeout 5 tcpdump -ni "$UP" -ee -c 1 "host $SRC and host $DST and icmp[icmptype]=8" 2>&1 | tail -1

echo
echo "=== summary ==="
echo -n "fwd req down/up/mgmt: "
grep -ci 'echo request' "/tmp/tr_${DOWN}.txt" 2>/dev/null || echo 0
echo -n " / "
grep -ci 'echo request' "/tmp/tr_${UP}.txt" 2>/dev/null || echo 0
echo -n " / "
grep -ci 'echo request' "/tmp/tr_${MGMT}.txt" 2>/dev/null || echo 0
echo -n "ret $DST>$SRC up: "
grep -c "$DST > $SRC" "/tmp/tr_${UP}.txt" 2>/dev/null || echo 0
echo -n " down: "
grep -c "$DST > $SRC" "/tmp/tr_${DOWN}.txt" 2>/dev/null || echo 0
echo -n " TE/reply up: "
grep -ciE 'echo reply|time exceeded' "/tmp/tr_${UP}.txt" 2>/dev/null || echo 0
echo -n " down: "
grep -ciE 'echo reply|time exceeded' "/tmp/tr_${DOWN}.txt" 2>/dev/null || echo 0
