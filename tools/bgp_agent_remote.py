"""现网 bgp-agent：写入 systemd 单元、重启并验收（deploy_light / 手工同步共用）。

RR 邻居不在 unit 里写死，由 OP BGP 管理页创建后调用 /api/rr/config。
"""
from __future__ import annotations

import os
from typing import Dict, Mapping


def bgp_agent_config_from_env() -> Dict[str, str]:
    return {
        "local_as": os.environ.get("LOCAL_AS", "63199").strip(),
        "router_id": os.environ.get("ROUTER_ID", "139.159.43.207").strip(),
        "redis_addr": os.environ.get("REDIS_ADDR", "localhost:6379").strip(),
        "rocksdb_path": os.environ.get("ROCKSDB_PATH", "/var/lib/bgp_agent/rocksdb").strip(),
        "api_addr": os.environ.get("API_ADDR", ":9179").strip(),
        "remote_dir": os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").strip(),
    }


def deploy_exec_timeout(*, remote_rebuild: bool) -> int:
    """SSH 脚本超时：健康检查最多约 600s，远程 go build 需更长。"""
    if remote_rebuild:
        return int(os.environ.get("MTR_DEPLOY_SSH_TIMEOUT", "2400"))
    return int(os.environ.get("MTR_DEPLOY_SSH_TIMEOUT", "720"))


def shell_sync_bgp_agent(cfg: Mapping[str, str], *, rebuild: bool = False) -> str:
    """生成在目标机执行的 bash：更新 unit、daemon-reload、restart、health + status。"""
    op_dir = cfg["remote_dir"]
    rocks = cfg["rocksdb_path"]
    rebuild_block = ""
    if rebuild:
        rebuild_block = f"""
export PATH=/usr/local/go/bin:$PATH
export GOPROXY=https://goproxy.cn,direct
export CGO_ENABLED=1
mkdir -p {rocks}
cd {op_dir}/bgp_agent
if [ -f go.mod ]; then
  go mod tidy 2>/dev/null || true
  go build -o bgp_agent -ldflags="-s -w" .
  echo "bgp_agent_rebuilt"
fi
"""
    return f"""
set -e
{rebuild_block}
chmod +x {op_dir}/bgp_agent/bgp_agent 2>/dev/null || true
mkdir -p {rocks}
mkdir -p /var/lib/bgp_agent
cat > /etc/systemd/system/bgp-agent.service <<'UNIT'
[Unit]
Description=BGP RX/TX Agent (GoBGP)
After=network.target redis-server.service
Wants=redis-server.service

[Service]
Type=simple
WorkingDirectory={op_dir}/bgp_agent
Environment=PATH=/usr/local/go/bin:/usr/bin:/bin
ExecStart={op_dir}/bgp_agent/bgp_agent \\
  -local-as {cfg["local_as"]} -router-id {cfg["router_id"]} \\
  -redis {cfg["redis_addr"]} -rocksdb {cfg["rocksdb_path"]} \\
  -api {cfg["api_addr"]}
Restart=always
RestartSec=10
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable bgp-agent 2>/dev/null || true
systemctl restart bgp-agent
echo "bgp-agent unit: local-as={cfg['local_as']} rid={cfg['router_id']} (RR via OP)"
systemctl is-active bgp-agent
AGENT_OK=0
for i in $(seq 1 120); do
  if curl -sf http://127.0.0.1:9179/health >/dev/null 2>&1; then
    echo " bgp-agent health OK (wait=${{i}}x5s)"
    AGENT_OK=1
    break
  fi
  sleep 5
done
if [ "$AGENT_OK" != 1 ]; then
  echo "bgp-agent health FAIL (timeout 600s)"
  exit 1
fi
echo "bgp-agent status:"
curl -sf http://127.0.0.1:9179/api/status | head -c 500 || {{ echo "bgp-agent status FAIL"; exit 1; }}
echo ""
OP_OK=0
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8808/health >/dev/null 2>&1; then
    OP_OK=1
    break
  fi
  sleep 2
done
if [ "$OP_OK" = 1 ]; then
  echo "bgp restore-agent:"
  curl -sf -X POST http://127.0.0.1:8808/api/bgp/restore-agent -H 'Content-Type: application/json' -d '{{}}' | head -c 800 || echo "restore warn"
  echo ""
fi
"""
