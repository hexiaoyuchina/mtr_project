#!/bin/bash
# 在 gobgp-rr 快照里找 8.8.8.8 / default
curl -sf 'http://127.0.0.1:9179/api/rib/routes?window=upstream&vrf=gobgp-rr&neighbor_ip=139.159.43.249&prefix=8.8.8.0/24&page_size=20' | python3 -c "
import sys,json
d=json.load(sys.stdin)
for r in d.get('routes') or []:
  if '8.8.8' in r.get('prefix',''):
    print(r)
print('total', d.get('total'))
" 2>/dev/null
# gobgp cli if available
command -v gobgp >/dev/null && gobgp -u 127.0.0.1 global rib -a ipv4 8.8.8.8/32 2>/dev/null | head -5 || true
