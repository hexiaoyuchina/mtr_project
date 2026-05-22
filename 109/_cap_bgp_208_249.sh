#!/bin/bash
# Read-only capture + state for 208 <-> spoof 249 (vbgp13915943249 / TX :1830)
set +e
VRF=vbgp13915943249
IV=iv249
SPOOF=139.159.43.249
PEER=139.159.43.208
DUR="${1:-12}"

echo "========== time / baseline =========="
date -Is
echo "Agent 208 neighbor:"
curl -s "http://127.0.0.1:9179/api/neighbors?vrf=$VRF" 2>/dev/null | python3 -c "
import sys,json
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='$PEER':
    print(n)
" 2>/dev/null

echo
echo "========== TCP before capture (${DUR}s) =========="
ss -tnp | grep -E '208|1830|:179 ' || true
ss -tnp state syn-sent,syn-recv,time-wait,fin-wait-1,fin-wait-2 2>/dev/null | grep -E '208|1830|249' || echo "(no transient 208/1830)"

echo
echo "========== tcpdump eno1np0 (iif downstream) port 179 host 208 =========="
timeout "$DUR" tcpdump -ni eno1np0 -vv \
  'tcp and host '"$PEER"' and (port 179 or port 1830)' 2>&1 | head -80

echo
echo "========== tcpdump enp59 uplink (should NOT be 208->249:179 for satellite) =========="
timeout 5 tcpdump -ni enp59s0f0np0 -vv \
  'tcp and host '"$PEER"' and port 179' 2>&1 | head -20

echo
echo "========== TCP after capture =========="
ss -tnp | grep -E '208|1830' || echo "(still no 208/1830 ESTAB)"

echo
echo "========== nft counter / hit (if any) =========="
nft list table inet mtr_bgp_sat_dnat 2>/dev/null | grep -A1 '249.*1830' || true

echo
echo "========== agent log last 208 =========="
journalctl -u bgp-agent --since '2 min ago' --no-pager 2>/dev/null | grep -iE '208|1830|passive|Active|estab' | tail -15
