#!/bin/bash
# 执行现网回退（优先用 apply 时生成的脚本）
set -e
if [ -x /tmp/mtr-op-return-rollback.sh ]; then
  bash /tmp/mtr-op-return-rollback.sh
  exit 0
fi
echo "no /tmp/mtr-op-return-rollback.sh, manual teardown"
RET_TABLE=2111
PREFIX=139.159.105.92/30
UP=enp59s0f0np0
while ip -4 rule del pref 31 iif "$UP" to "$PREFIX" lookup "$RET_TABLE" 2>/dev/null; do :; done
while ip -4 rule del pref 29 to "$PREFIX" lookup "$RET_TABLE" 2>/dev/null; do :; done
ip route flush table "$RET_TABLE" 2>/dev/null || true
echo "manual rollback done"
