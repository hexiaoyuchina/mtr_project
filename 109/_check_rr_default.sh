#!/bin/bash
RR=139.159.43.249
for p in 0.0.0.0/0 8.8.8.8/32 0.0.0.0/1 128.0.0.0/1; do
  echo "=== $p ==="
  curl -sf "http://127.0.0.1:9179/api/rib/routes?window=upstream&vrf=gobgp-rr&neighbor_ip=${RR}&prefix=${p}&page_size=3" | python3 -m json.tool 2>/dev/null | head -25
done
