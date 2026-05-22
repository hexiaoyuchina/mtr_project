#!/bin/bash
set +e
PEER=139.159.43.208
echo "=== wait for next passive attempt: 45s on ALL ifaces tcp 179 and host $PEER ==="
timeout 45 tcpdump -ni any -c 50 "(tcp port 179 or tcp port 1830) and host $PEER" 2>&1

echo "=== 15s any iface host $PEER all tcp ==="
timeout 15 tcpdump -ni any -c 30 "tcp and host $PEER" 2>&1

echo "=== ss + last log ==="
ss -tnp | grep -E '208|1830' || true
journalctl -u bgp-agent -n 5 --no-pager 2>/dev/null | grep 208
