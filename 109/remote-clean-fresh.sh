#!/bin/bash
# 现网 VR：停服务并清理代码与三套存储（干净重建用）
set -e
REMOTE="${MTR_OP_REMOTE_DIR:-/root/mtr_op}"
ROCKS="${ROCKSDB_PATH:-/var/lib/bgp_agent/rocksdb}"

echo "=== remote-clean-fresh: REMOTE=$REMOTE ROCKS=$ROCKS ==="

systemctl stop bgp-agent mtr-op 2>/dev/null || true
systemctl disable mtr-op 2>/dev/null || true
pkill -f bgp_agent 2>/dev/null || true
pkill -f 'uvicorn app.main' 2>/dev/null || true
pkill -f 'uvicorn service.app.main' 2>/dev/null || true
pkill -f mtr_spoof_nfqueue 2>/dev/null || true
pkill -f te_rewrite_nfqueue 2>/dev/null || true
pkill -f arp_spoof_daemon 2>/dev/null || true
sleep 2

nft delete table inet mtr_spoof 2>/dev/null || true
nft delete table inet mtr_te 2>/dev/null || true
nft delete table inet nat_sat_bgp 2>/dev/null || true

rm -rf "$REMOTE"
mkdir -p "$REMOTE"
mkdir -p "$(dirname "$ROCKS")"

rm -f "$REMOTE"/*.db 2>/dev/null || true
rm -rf "${ROCKS:?}"/* 2>/dev/null || true

if command -v redis-cli >/dev/null 2>&1; then
  redis-cli FLUSHDB 2>/dev/null || true
fi

: > /tmp/mtr_op.log 2>/dev/null || true
: > /tmp/te_rewrite_nfqueue.log 2>/dev/null || true
: > /tmp/arp_spoof_daemon.log 2>/dev/null || true

echo "remote-clean-fresh_ok"
