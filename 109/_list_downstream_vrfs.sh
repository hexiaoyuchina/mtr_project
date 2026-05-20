#!/bin/bash
DOWN="${MTR_OP_DOWNSTREAM_IFACE:-eno1np0}"
echo "========== VRF devices (all) =========="
ip -br link show type vrf 2>/dev/null || ip -br link | grep -E '^vbgp|^vrf' || true
echo
echo "========== ipvlan on $DOWN =========="
ip -br link | grep "@${DOWN}" || ip link | grep "@${DOWN}" | head -30
echo
echo "========== vrf -> table (rt_tables / link) =========="
for v in $(ip -o link show type vrf 2>/dev/null | awk -F': ' '{print $2}' | awk '{print $1}'); do
  tbl=$(ip -d link show "$v" 2>/dev/null | sed -n 's/.*table \([0-9]*\).*/\1/p' | head -1)
  echo "  $v  rt_table=$tbl"
done
echo
echo "========== ip rule (vbgp / 1000+) sample =========="
ip -4 rule list | grep -E 'vbgp|lookup [0-9]{4,}' | head -25
echo
echo "========== sample: vbgp for 249 / 208 (if exist) =========="
for v in vbgp13915943249 default; do
  echo "--- vrf $v ---"
  ip link show "$v" 2>/dev/null | head -2 || echo "  (no such vrf)"
  ip route show vrf "$v" 2>/dev/null | head -8 || true
done
echo
echo "========== main vs 2110 (transit) =========="
ip route show table 2110 2>/dev/null | head -5
ip -4 rule list | grep -E '^30:|^31:|^29:' || true
