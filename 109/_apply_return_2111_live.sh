#!/bin/bash
# 现网一次性：回程 table 2111 + pref 29/31（不改仓库代码）
# 回退：bash /tmp/mtr-op-return-rollback.sh
set -euo pipefail

DOWN="${MTR_OP_DOWNSTREAM_IFACE:-eno1np0}"
UP="${MTR_BGP_RR_UPLINK_IFACE:-enp59s0f0np0}"
PEER="${MTR_BGP_IPVLAN_PEER_IP:-139.159.43.208}"
RET_TABLE="${MTR_DOWNSTREAM_RETURN_TABLE:-2111}"
PREFIX="${MTR_DOWNSTREAM_RETURN_PREFIX:-139.159.105.92/30}"
RET_PREF="${MTR_DOWNSTREAM_RETURN_RULE_PREF:-31}"
TO_PREF="${MTR_DOWNSTREAM_TO_CLIENT_RULE_PREF:-29}"
STAMP="/tmp/mtr-return-applied-$(date +%Y%m%d%H%M%S).txt"
ROLLBACK="/tmp/mtr-op-return-rollback.sh"

echo "========== apply return path $(date -Is) =========="
echo "DOWN=$DOWN UP=$UP RET_TABLE=$RET_TABLE PREFIX=$PREFIX"
echo "RET_PREF=$RET_PREF TO_PREF=$TO_PREF"
echo

# 记录应用前快照
{
  echo "=== before rules 29/31 ==="
  ip -4 rule list | grep -E '^29:|^31:' || true
  echo "=== before table $RET_TABLE ==="
  ip route show table "$RET_TABLE" 2>/dev/null || true
  echo "=== before main 105 routes ==="
  ip route show table main | grep -E '105\.|43\.208' || true
  echo "=== route get 105.94 return ==="
  ip route get 139.159.105.94 from 8.8.8.8 iif "$UP" 2>&1 || true
} | tee "$STAMP"

# 生成回退脚本
cat >"$ROLLBACK" <<RB
#!/bin/bash
set -e
echo "rollback mtr return 2111 $(date -Is)"
while ip -4 rule del pref $RET_PREF iif $UP to $PREFIX lookup $RET_TABLE 2>/dev/null; do :; done
while ip -4 rule del pref $TO_PREF to $PREFIX lookup $RET_TABLE 2>/dev/null; do :; done
ip route flush table $RET_TABLE 2>/dev/null || true
echo "rollback done; verify:"
ip route get 139.159.105.94 from 8.8.8.8 iif $UP 2>&1 | head -2
ip -4 rule list | grep -E '^29:|^31:' || echo "(no 29/31)"
RB
chmod +x "$ROLLBACK"

# 清理旧 rule（若曾下发过对称回程）
while ip -4 rule del pref "$RET_PREF" iif "$UP" to 139.159.105.94/32 lookup "$RET_TABLE" 2>/dev/null; do :; done
while ip -4 rule del pref "$TO_PREF" to 139.159.105.94/32 lookup "$RET_TABLE" 2>/dev/null; do :; done
while ip -4 rule del pref "$RET_PREF" iif "$UP" to "$PREFIX" lookup "$RET_TABLE" 2>/dev/null; do :; done
while ip -4 rule del pref "$TO_PREF" to "$PREFIX" lookup "$RET_TABLE" 2>/dev/null; do :; done

# 主表：去掉可能抢路的 105 主机路由（仅删我们关心的）
for r in 139.159.105.94/32 139.159.105.92/30; do
  ip route del "$r" dev "$DOWN" table main 2>/dev/null || true
  ip route del "$r" dev "$DOWN" 2>/dev/null || true
done

# table 2111 路由（不用 via 208）
ip route replace table "$RET_TABLE" "$PREFIX" dev "$DOWN" scope link
ip route replace table "$RET_TABLE" "$PEER/32" dev "$DOWN" scope link

# 策略规则
ip -4 rule add pref "$RET_PREF" iif "$UP" to "$PREFIX" lookup "$RET_TABLE"
ip -4 rule add pref "$TO_PREF" to "$PREFIX" lookup "$RET_TABLE"

echo
echo "========== after apply =========="
echo "=== table $RET_TABLE ==="
ip route show table "$RET_TABLE"
echo "=== rules 29/31 ==="
ip -4 rule list | grep -E '^29:|^31:' || true
echo "=== route get forward 105.94 iif $DOWN ==="
ip route get 8.8.8.8 from 139.159.105.94 iif "$DOWN" 2>&1 | head -2
echo "=== route get return 105.94 iif $UP ==="
ip route get 139.159.105.94 from 8.8.8.8 iif "$UP" 2>&1 | head -2
echo "=== route get local to 105.94 ==="
ip route get 139.159.105.94 2>&1 | head -2
echo
echo "snapshot: $STAMP"
echo "rollback:  bash $ROLLBACK"
