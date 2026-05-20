#!/bin/bash
# 公网对 105.94 做 mtr/traceroute 时常见探测：ICMP / UDP
SRC=139.159.105.94
DOWN=eno1np0
UP=enp59s0f0np0
SEC=45
FILTER="host $SRC and (icmp or udp)"

echo "========== $(date -Is) ICMP/UDP host $SRC ${SEC}s =========="
ip route get "$SRC" 2>&1 | head -1
ip route get "$SRC" from 1.1.1.1 iif "$UP" 2>&1 | head -1
echo

for dev in "$DOWN" "$UP"; do
  timeout "$SEC" tcpdump -ni "$dev" -l -n "$FILTER" 2>/dev/null >"/tmp/mtr_${dev}.txt" &
done
wait 2>/dev/null || true

for dev in "$DOWN" "$UP"; do
  f="/tmp/mtr_${dev}.txt"
  echo "=== $dev ==="
  if [ ! -s "$f" ]; then echo "  (无 ICMP/UDP)"; continue; fi
  wc -l <"$f" | xargs echo "  包数:"
  grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ > [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "$f" | sort | uniq -c | sort -rn | head -20
  echo -n "  echo req: "; grep -ci 'echo request' "$f" || echo 0
  echo -n "  echo rep:  "; grep -ci 'echo reply' "$f" || echo 0
  echo -n "  time exc: "; grep -ci 'time exceeded' "$f" || echo 0
  echo -n "  port unreach: "; grep -ci 'port unreachable' "$f" || echo 0
  echo "  样例:"
  head -12 "$f" | sed 's/^/    /'
done

echo
echo "=== 入向(外网->105.94) uplink vs down ==="
echo -n "  上联入: "; grep -cE '> '"$SRC" "$f" 2>/dev/null; for f in /tmp/mtr_${UP}.txt; do grep -cE '> '"$SRC" "$f" 2>/dev/null || echo 0; done
echo -n "  下联出(转发): "; grep -cE '> '"$SRC" /tmp/mtr_${DOWN}.txt 2>/dev/null || echo 0
echo -n "  下联入(105.94发出): "; grep -cE "^[^ ]+ [^ ]+ IP $SRC >" /tmp/mtr_${DOWN}.txt 2>/dev/null || grep -c "$SRC >" /tmp/mtr_${DOWN}.txt 2>/dev/null || echo 0
echo -n "  上联出(105.94发出): "; grep -c "$SRC >" /tmp/mtr_${UP}.txt 2>/dev/null || echo 0
