#!/bin/bash
# 在 VR 上执行（依赖已通过 deploy 或手动 apt/pip 装好）
set -e
REMOTE="${MTR_OP_HOME:-/root/mtr_op}"
cd "$REMOTE"
export MTR_OP_DB="$REMOTE/data.db"
export MTR_OP_NFT="$REMOTE/nft_mtr_te.nft"
export MTR_TE_REWRITE_SCRIPT="${MTR_TE_REWRITE_SCRIPT:-$REMOTE/te_rewrite_nfqueue.py}"

pkill -f 'uvicorn app.main:app' 2>/dev/null || true
pkill -f mtr_spoof_nfqueue.py 2>/dev/null || true
sleep 1

nft delete table inet mtr_spoof 2>/dev/null || true
nft delete table inet mtr_te 2>/dev/null || true
nft -f "$REMOTE/nft_mtr_te.nft" || echo "WARN: nft load failed"

if [ -x ./venv/bin/pip ]; then
  ./venv/bin/pip install -q -r requirements.txt || true
fi
: > /tmp/mtr_op.log
if [ -x "$REMOTE/venv/bin/uvicorn" ]; then
  UV="$REMOTE/venv/bin/uvicorn"
else
  UV="python3 -m uvicorn"
fi
nohup $UV app.main:app --host 0.0.0.0 --port 8808 >> /tmp/mtr_op.log 2>&1 &
sleep 5

curl -s -S "http://127.0.0.1:8808/health" && echo ""
curl -s -S "http://127.0.0.1:8808/api/hop-rules" && echo ""
pgrep -af te_rewrite || true
echo "OK: OP started (TE rewrite via te_rewrite_sync when hijack_enabled)"
