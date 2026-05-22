#!/bin/bash
set +e
PEER=139.159.43.208
date -Is
echo "=== ping vrf iv249 -> $PEER ==="
ip vrf exec vbgp13915943249 ping -c2 -W2 -I iv249 "$PEER"
echo "=== ping main -> $PEER ==="
ping -c2 -W2 "$PEER"
echo "=== 25s tcpdump any host $PEER ==="
timeout 25 tcpdump -ni any -c 20 "host $PEER" 2>&1
echo "=== journal 208 ==="
journalctl -u bgp-agent -n 12 --no-pager 2>/dev/null | grep 208
echo "=== neighbor ==="
curl -s http://127.0.0.1:9179/api/neighbors 2>/dev/null
