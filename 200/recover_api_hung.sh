#!/bin/bash
# 在 Linux 200 本机控制台执行：OP/Agent API 无响应（/health 也超时）时恢复
set -e
REMOTE="${MTR_OP_REMOTE_DIR:-/root/mtr_op}"
echo "=== 结束可能卡死的 uvicorn / 大任务 ==="
pkill -f 'uvicorn app.main' 2>/dev/null || true
pkill -f 'uvicorn service.app.main' 2>/dev/null || true
sleep 2
echo "=== 重启 bgp-agent ==="
systemctl restart bgp-agent.service 2>/dev/null || true
sleep 5
echo "=== 重启 OP（remote-restart）==="
cd "$REMOTE"
export MTR_BGP_NEIGHBORS_FAST_LIST=1
export MTR_BGP_NEIGHBORS_AGENT_TIMEOUT=12
bash ./remote-restart.sh
echo "=== 探测 ==="
curl -sf -m 5 http://127.0.0.1:8808/health && echo " op_ok"
curl -sf -m 5 http://127.0.0.1:9179/health && echo " agent_ok"
curl -sf -m 20 -o /dev/null -w "neighbors:%{http_code} %{time_total}s\n" http://127.0.0.1:8808/api/bgp/neighbors
