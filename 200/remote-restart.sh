#!/bin/bash
# 在 Linux 200 上执行：加载 lab 环境并重启 mtr-op / NFQUEUE（由 deploy.py 注入 REMOTE）
set -e
REMOTE="${MTR_OP_REMOTE_DIR:-/root/mtr_op}"
cd "$REMOTE"

export GOBGP_AGENT_URL=http://127.0.0.1:9179
export MTR_OP_DB="$REMOTE/data.db"
export MTR_OP_NFT="$REMOTE/nft_mtr_spoof.nft"
export MTR_OP_DATA="$REMOTE/data"
export LOCAL_AS="${LOCAL_AS:-63199}"
export ROUTER_ID="${ROUTER_ID:-10.133.153.200}"
export MTR_DOWNSTREAM_REMOTE_AS="${MTR_DOWNSTREAM_REMOTE_AS:-63199}"
export MTR_BGP_IPVLAN_AUTO="${MTR_BGP_IPVLAN_AUTO:-1}"
export MTR_BGP_IPVLAN_BASE_IFACE="${MTR_BGP_IPVLAN_BASE_IFACE:-ens192}"
export MTR_BGP_RR_UPLINK_IFACE="${MTR_BGP_RR_UPLINK_IFACE:-ens224}"
export MTR_BGP_IPVLAN_PEER_IP="${MTR_BGP_IPVLAN_PEER_IP:-10.133.152.204}"
export MTR_SATELLITE_PEER_IP="${MTR_SATELLITE_PEER_IP:-10.133.152.204}"
export MTR_SATELLITE_PHY_VRF="${MTR_SATELLITE_PHY_VRF:-default}"
export MTR_AUTO_SATELLITE_VRF="${MTR_AUTO_SATELLITE_VRF:-note}"
export MTR_AUTO_SATELLITE_VRF_NOTE="${MTR_AUTO_SATELLITE_VRF_NOTE:-BGPSAT}"
export MTR_BGP_RIB_SYNC="${MTR_BGP_RIB_SYNC:-1}"
export MTR_BGP_RIB_SYNC_SEC="${MTR_BGP_RIB_SYNC_SEC:-60}"
export MTR_PROBE_SSH_HOST="${MTR_PROBE_SSH_HOST:-10.133.151.200}"
export MTR_TE_PROBE_RETURN_VIA_200="${MTR_TE_PROBE_RETURN_VIA_200:-0}"
export MTR_TE_PROBE_SRC="${MTR_TE_PROBE_SRC:-10.133.152.204}"
export MTR_TE_RETURN_IP="${MTR_TE_RETURN_IP:-10.133.152.200}"
export MTR_TE_REWRITE_IIF="${MTR_TE_REWRITE_IIF:-ens224}"
export MTR_TE_REWRITE_PEER_HOSTS="${MTR_TE_REWRITE_PEER_HOSTS:-}"
export MTR_TE_REWRITE_PEER_QUEUE="${MTR_TE_REWRITE_PEER_QUEUE:-2}"
export MTR_TE_REWRITE_PEER_SCRIPT="${MTR_TE_REWRITE_PEER_SCRIPT:-/root/te_rewrite_nfqueue.py}"
# 由 deploy 注入，供 peer SSH（勿提交明文密码到仓库）
export MTR_OP_SSH_PASSWORD="${MTR_OP_SSH_PASSWORD:-}"
export MTR_BGP_ROLE_MAP="${MTR_BGP_ROLE_MAP:-10.133.153.204:rr,10.133.152.204:downstream}"
export MTR_BGP_DB_PRESETS="${MTR_BGP_DB_PRESETS:-default:10.133.153.204:rr,default:10.133.152.204:downstream}"
export MTR_BGP_NEIGHBORS_FAST_LIST="${MTR_BGP_NEIGHBORS_FAST_LIST:-1}"
export MTR_BGP_NEIGHBORS_AGENT_TIMEOUT="${MTR_BGP_NEIGHBORS_AGENT_TIMEOUT:-12}"

if [ -x ./venv/bin/python ]; then PY=./venv/bin/python; else PY=python3; fi
$PY - <<'INITSCHEMA'
import os, sys
sys.path.insert(0, os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op"))
from pathlib import Path
from app import storage
db = Path(os.environ["MTR_OP_DB"])
conn = storage.connect(db)
storage.init_schema(conn)
conn.close()
print("schema_ok")
INITSCHEMA

systemctl stop frr 2>/dev/null || true
systemctl disable frr 2>/dev/null || true

# 避免与错误 unit（service.app.main）双实例抢 8808
systemctl stop mtr-op 2>/dev/null || true
systemctl disable mtr-op 2>/dev/null || true
pkill -f 'uvicorn service.app.main' 2>/dev/null || true
pkill -f 'uvicorn app.main:app' 2>/dev/null || true
pkill -f mtr_spoof_nfqueue 2>/dev/null || true
sleep 1
nft delete table inet mtr_spoof 2>/dev/null || true
[ -f nft_mtr_spoof.nft ] && nft -f nft_mtr_spoof.nft || echo "WARN: nft skipped"

: > /tmp/mtr_op.log
if [ -x ./venv/bin/uvicorn ]; then UV=./venv/bin/uvicorn; else UV='python3 -m uvicorn'; fi
nohup $UV app.main:app --host 0.0.0.0 --port 8808 >> /tmp/mtr_op.log 2>&1 &
sleep 5
for i in $(seq 1 12); do
  curl -sf http://127.0.0.1:8808/health >/dev/null && break
  sleep 1
done

: > /tmp/mtr_spoof_nfqueue.log
nohup $PY mtr_spoof_nfqueue.py --op-db "$MTR_OP_DB" --verbose >> /tmp/mtr_spoof_nfqueue.log 2>&1 &
pkill -f arp_spoof_daemon.py 2>/dev/null || true
: > /tmp/arp_spoof_daemon.log
if [ -f scripts/arp_spoof_daemon.py ]; then
  nohup $PY scripts/arp_spoof_daemon.py --op-db "$MTR_OP_DB" >> /tmp/arp_spoof_daemon.log 2>&1 &
elif [ -f arp_spoof_daemon.py ]; then
  nohup $PY arp_spoof_daemon.py --op-db "$MTR_OP_DB" >> /tmp/arp_spoof_daemon.log 2>&1 &
else
  echo "WARN: arp_spoof_daemon.py missing — ARP 引流 GARP 不会发送"
fi
sleep 2
curl -sS http://127.0.0.1:8808/health; echo
pgrep -af 'uvicorn app.main|mtr_spoof|arp_spoof' || true
