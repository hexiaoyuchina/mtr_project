#!/bin/bash
# Doc-aligned checklist + 90s capture (BGP_RXTX §6.5, BGP_SATELLITE §3.4)
set +e
VRF=vbgp13915943249
IV=iv249
SPOOF=139.159.43.249
PEER=139.159.43.208
UP=enp59s0f0np0
DOWN=eno1np0

echo "========== DOC §6.5 / §3.4 checklist =========="
echo "--- nft DNAT 249 ---"
nft list chain inet mtr_bgp_sat_dnat prerouting 2>/dev/null | grep "$SPOOF" || echo "MISSING dnat for $SPOOF"

echo "--- ip rule 249 / 208 ---"
ip -4 rule list | grep -E "249|30449" | head -10

echo "--- route get 208 from 249 (expect iv249, table 30449) ---"
ip route get "$PEER" from "$SPOOF" 2>&1

echo "--- TX :1830 listen ---"
ss -tlnp | grep ':1830'

echo "--- 249 on interfaces (must NOT be on $UP for RR session) ---"
ip -br addr | grep 249

echo "--- passive env ---"
systemctl show bgp-agent -p Environment 2>/dev/null | tr ' ' '\n' | grep -iE 'PASSIVE|IPVLAN|DNAT|RR_SPOOF'

echo "--- agent neighbor 208 ---"
curl -s "http://127.0.0.1:9179/api/neighbors?vrf=$VRF" 2>/dev/null

echo "--- vrf routes ---"
ip route show vrf "$VRF" | head -8

echo
echo "========== 90s capture: downstream $DOWN (208 or dport 179/1830) =========="
timeout 90 tcpdump -ni "$DOWN" -vv -c 60 \
  '(tcp port 179 or tcp port 1830) and (host '"$PEER"' or host '"$SPOOF"')' 2>&1

echo
echo "========== 30s capture: uplink $UP (should be empty for 208->249 spoof) =========="
timeout 30 tcpdump -ni "$UP" -vv -c 20 \
  '(tcp port 179) and host '"$PEER" 2>&1

echo
echo "========== TCP + journal tail =========="
ss -tnp | grep -E '208|1830|:179 ' || true
journalctl -u bgp-agent -n 6 --no-pager 2>/dev/null | grep -iE '208|passive|1830|estab' || true
