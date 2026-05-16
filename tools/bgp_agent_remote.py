"""现网 bgp-agent：写入 systemd 单元、重启并验收（deploy_light / 手工同步共用）。"""
from __future__ import annotations

import os
from typing import Dict, Mapping


def bgp_agent_config_from_env() -> Dict[str, str]:
    return {
        "rr_addr": os.environ.get("RR_ADDR", "139.159.43.249").strip(),
        "rr_as": os.environ.get("RR_AS", "63199").strip(),
        "local_as": os.environ.get("LOCAL_AS", "63199").strip(),
        "router_id": os.environ.get("ROUTER_ID", "101.89.68.109").strip(),
        "redis_addr": os.environ.get("REDIS_ADDR", "localhost:6379").strip(),
        "rocksdb_path": os.environ.get("ROCKSDB_PATH", "/var/lib/bgp_agent/rocksdb").strip(),
        "api_addr": os.environ.get("API_ADDR", ":9179").strip(),
        "remote_dir": os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").strip(),
    }


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
mkdir -p {rocks}
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
  -rr {cfg["rr_addr"]} -rr-as {cfg["rr_as"]} \\
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
sleep 3
echo "bgp-agent unit: rr={cfg['rr_addr']} as={cfg['local_as']} rid={cfg['router_id']}"
systemctl is-active bgp-agent
curl -sf http://127.0.0.1:9179/health && echo " bgp-agent health OK" || {{ echo "bgp-agent health FAIL"; exit 1; }}
echo "bgp-agent status:"
curl -sf http://127.0.0.1:9179/api/status || {{ echo "bgp-agent status FAIL"; exit 1; }}
echo ""
"""
