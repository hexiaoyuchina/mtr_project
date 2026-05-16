#!/bin/bash
# 在 Linux 200 上执行（依赖已通过 deploy 或手动 apt/pip 装好）
set -e
REMOTE="${MTR_OP_HOME:-/root/mtr_op}"
cd "$REMOTE"
export MTR_OP_DB="$REMOTE/data.db"
export MTR_OP_NFT="$REMOTE/nft_mtr_spoof.nft"

pkill -f 'uvicorn app.main:app' 2>/dev/null || true
pkill -f mtr_spoof_nfqueue.py 2>/dev/null || true
sleep 1

nft delete table inet mtr_spoof 2>/dev/null || true
nft -f "$REMOTE/nft_mtr_spoof.nft" || echo "WARN: nft load failed"

: > /tmp/mtr_spoof_nfqueue.log
export MTR_PROBE_SSH_HOST="${MTR_PROBE_SSH_HOST:-}"
export MTR_PROBE_VRF_EXEC="${MTR_PROBE_VRF_EXEC:-ip vrf exec vrf2102}"
export MTR_PROBE_LOCAL_VRF_EXEC="${MTR_PROBE_LOCAL_VRF_EXEC:-}"
export MTR_PROBE_MTR_COUNT="${MTR_PROBE_MTR_COUNT:-15}"
nohup env MTR_PROBE_SSH_HOST="$MTR_PROBE_SSH_HOST" MTR_PROBE_VRF_EXEC="$MTR_PROBE_VRF_EXEC" MTR_PROBE_LOCAL_VRF_EXEC="$MTR_PROBE_LOCAL_VRF_EXEC" MTR_PROBE_MTR_COUNT="$MTR_PROBE_MTR_COUNT" python3 "$REMOTE/mtr_spoof_nfqueue.py" --op-db "$REMOTE/data.db" --verbose >> /tmp/mtr_spoof_nfqueue.log 2>&1 &
sleep 2

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
sleep 3

curl -s -S "http://127.0.0.1:8808/health" && echo ""
curl -s -S "http://127.0.0.1:8808/api/hop-rules" && echo ""
nft list chain inet mtr_spoof prerouting 2>/dev/null || true
echo "OK: http://101.89.68.109:8808/"
