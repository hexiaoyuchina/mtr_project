#!/bin/bash
# Linux 200：上游 RR 走 ens224/vrf2103 时的策略路由（GoBGP RX 在主表发源 153.200）
set -e
ip rule del pref 50 from 10.133.153.200/32 2>/dev/null || true
ip rule add pref 50 from 10.133.153.200/32 lookup 2103
ip route replace table 2103 10.133.153.204/32 dev ens224 src 10.133.153.200
echo "network-prereq ok"
