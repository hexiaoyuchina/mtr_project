#!/bin/bash
set -e
nft add table inet bgp_sat_dnat 2>/dev/null || true
nft add chain inet bgp_sat_dnat prerouting '{ type nat hook prerouting priority -100; policy accept; }' 2>/dev/null || true
nft flush chain inet bgp_sat_dnat prerouting 2>/dev/null || true
nft add rule inet bgp_sat_dnat prerouting ip daddr 10.133.152.233 tcp dport 179 redirect to :1792
nft list chain inet bgp_sat_dnat prerouting
sleep 3
curl -s http://127.0.0.1:9179/api/peers/freeze-status
echo
ss -tnp state established 2>/dev/null | grep -E '152.233|152.204|1792' || true
