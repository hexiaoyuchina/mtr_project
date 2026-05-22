#!/bin/bash
set +e
PEER=139.159.43.208
echo "=== 30s eno1np0 any tcp host $PEER ==="
timeout 30 tcpdump -ni eno1np0 -c 40 "tcp and host $PEER" 2>&1

echo "=== 20s eno1np0 tcp port 179 (all peers) ==="
timeout 20 tcpdump -ni eno1np0 -c 15 "tcp port 179" 2>&1

echo "=== ss 208 / syn-sent ==="
ss -tnp | grep 208 || echo "no tcp with 208"
ss -tnp state syn-sent 2>/dev/null | head -8

echo "=== agent all neighbors ==="
curl -s http://127.0.0.1:9179/api/neighbors 2>/dev/null

echo "=== journal 208 5min ==="
journalctl -u bgp-agent --since "5 min ago" --no-pager 2>/dev/null | grep 208 | tail -30
