#!/bin/bash
set -e
timeout 12 tcpdump -ni ens224 'icmp and host 210.73.209.82' -c 8 2>&1 &
T1=$!
timeout 12 tcpdump -ni ens160 'icmp and host 210.73.209.82' -c 8 2>&1 &
T2=$!
sleep 1
ssh -o StrictHostKeyChecking=no root@10.133.151.201 \
  'mtr -4 -r -n -m 5 -c 2 -a 10.133.152.204 -I ens192 210.73.209.82' 2>&1 | head -12
wait $T1 $T2 2>/dev/null || true
