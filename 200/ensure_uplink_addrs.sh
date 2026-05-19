#!/bin/bash
# ens224 上确保 RR 本端 153.200（供 remote-network-prereq / RX）
set -e
ip link set ens224 up
ip link set ens192 up
if ! ip -4 addr show dev ens224 | grep -q '10.133.153.200/'; then
  ip addr add 10.133.153.200/32 dev ens224 2>/dev/null || true
fi
bash /root/mtr_op/remote-network-prereq.sh
ip -br addr show ens224 ens192 iv204 2>/dev/null || true
