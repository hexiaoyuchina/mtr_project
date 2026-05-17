#!/bin/bash
VRF=vbgp13915943249
PEER=139.159.43.208
SPOOF=139.159.43.249
echo "table 30449:"; ip route show table 30449
echo "vrf route:"; ip route show vrf $VRF 2>/dev/null || true
echo "links:"; ip -br link | grep -E 'vbgp|iv249|enp59'
echo "neigh:"; ip neigh show dev enp59s0f0np0 | grep 139.159 || true
echo "get208:"; ip route get $PEER
echo "rules:"; ip -4 rule show | head -25
