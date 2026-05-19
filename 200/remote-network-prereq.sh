#!/bin/bash
# Linux 200：上游 RR 走 ens224/table2103（GoBGP RX 源 153.200 → ROS 153.204）
# 下游 ens192 也在 iv204 上挂 153.204/32 时，主表会判 local，须用更高优先级 rule 强制走上联口。
set -e
RR_DST="${RR_ADDR:-10.133.153.204}"
RR_SRC="${ROUTER_ID:-10.133.153.200}"
UPLINK="${MTR_BGP_RR_UPLINK_IFACE:-ens224}"

ip link set "${UPLINK}" up 2>/dev/null || true
if ! ip -4 addr show dev "${UPLINK}" | grep -q "${RR_SRC}/"; then
  ip addr add "${RR_SRC}/32" dev "${UPLINK}" 2>/dev/null || true
fi

# setup_201_via_200_mtr 写入的 43/44 会盖住「from 冒充源 → 卫星表」，导致下游 BGP Active
ip rule del pref 44 from 10.133.152.0/24 2>/dev/null || true
ip rule del pref 43 iif "${MTR_BGP_IPVLAN_BASE_IFACE:-ens192}" lookup 2103 2>/dev/null || true
ip rule del pref 43 2>/dev/null || true

ip rule del pref 45 from "${RR_SRC}/32" to "${RR_DST}/32" 2>/dev/null || true
ip rule add pref 45 from "${RR_SRC}/32" to "${RR_DST}/32" lookup 2103
ip rule del pref 50 from "${RR_SRC}/32" 2>/dev/null || true
ip rule add pref 50 from "${RR_SRC}/32" lookup 2103
ip route replace table 2103 "${RR_DST}/32" dev "${UPLINK}" src "${RR_SRC}"
echo "network-prereq ok (${RR_SRC} -> ${RR_DST} via ${UPLINK})"
