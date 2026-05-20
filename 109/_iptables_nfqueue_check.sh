#!/bin/bash
echo '=== all iptables NFQUEUE ==='
iptables-save 2>/dev/null | grep -i nfqueue || true
echo '=== nft full mtr ==='
nft list ruleset 2>/dev/null | grep -E 'mtr|queue|icmp|echo' || true
