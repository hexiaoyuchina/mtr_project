#!/bin/bash
set +e
DOWN="${MTR_OP_DOWNSTREAM_IFACE:-eno1np0}"
UP="${MTR_BGP_RR_UPLINK_IFACE:-enp59s0f0np0}"
echo "=== ip rule 29/30 ==="
ip -4 rule list | grep -E '^29:|^30:' || echo "MISSING rules"
echo "=== table 2110 ==="
ip route show table 2110
echo "=== table 2111 ==="
ip route show table 2111
echo "=== forward get ==="
ip route get 8.8.8.8 from 139.159.105.94 iif "$DOWN"
echo "=== return get ==="
ip route get 139.159.105.94 from 8.8.8.8 iif "$UP"
echo "=== local get 105.94 ==="
ip route get 139.159.105.94
echo "=== neigh ==="
ip neigh show dev "$DOWN" | grep -E '105\.94|43\.208' || true
echo "=== sysctl ==="
sysctl net.ipv4.ip_forward net.ipv4.conf.all.rp_filter 2>/dev/null
echo "=== mangle FORWARD head ==="
iptables -t mangle -S FORWARD 2>/dev/null | head -6
