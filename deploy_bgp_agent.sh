#!/bin/bash
# BGP Agent 编译和部署脚本

set -e

echo "=========================================="
echo "BGP RX/TX分离架构 - 部署脚本"
echo "=========================================="

# 配置参数
RR_ADDR="${RR_ADDR:-139.159.43.249}"
RR_AS="${RR_AS:-63199}"
LOCAL_AS="${LOCAL_AS:-63199}"
ROUTER_ID="${ROUTER_ID:-101.89.68.109}"
REDIS_ADDR="${REDIS_ADDR:-localhost:6379}"
ROCKSDB_PATH="${ROCKSDB_PATH:-/var/lib/bgp_agent/rocksdb}"
API_ADDR="${API_ADDR:-:9179}"

echo "配置参数："
echo "  RR地址: $RR_ADDR"
echo "  RR AS: $RR_AS"
echo "  本地AS: $LOCAL_AS"
echo "  Router ID: $ROUTER_ID"
echo "  Redis: $REDIS_ADDR"
echo "  RocksDB: $ROCKSDB_PATH"
echo "  API监听: $API_ADDR"
echo ""

# 创建目录
echo "创建数据目录..."
sudo mkdir -p $(dirname $ROCKSDB_PATH)
sudo chown -R $USER:$USER $(dirname $ROCKSDB_PATH)

# 检查Redis
echo "检查Redis..."
if ! redis-cli -h localhost ping > /dev/null 2>&1; then
    echo "警告: Redis未运行，请先启动Redis"
    echo "  sudo systemctl start redis-server"
    exit 1
fi
echo "  ✓ Redis运行正常"

# 进入bgp_agent目录
cd service/bgp_agent

# 下载依赖
echo "下载Go依赖..."
go mod download
echo "  ✓ 依赖下载完成"

# 编译
echo "编译BGP Agent..."
go build -o bgp_agent -ldflags="-s -w" .
echo "  ✓ 编译完成"

# 创建systemd服务文件
echo "创建systemd服务..."
sudo tee /etc/systemd/system/bgp-agent.service > /dev/null <<EOF
[Unit]
Description=BGP RX/TX Agent
After=network.target redis.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
Environment="RR_ADDR=$RR_ADDR"
Environment="RR_AS=$RR_AS"
Environment="LOCAL_AS=$LOCAL_AS"
Environment="ROUTER_ID=$ROUTER_ID"
Environment="REDIS_ADDR=$REDIS_ADDR"
Environment="ROCKSDB_PATH=$ROCKSDB_PATH"
Environment="API_ADDR=$API_ADDR"
ExecStart=$(pwd)/bgp_agent -rr $RR_ADDR -rr-as $RR_AS -local-as $LOCAL_AS -router-id $ROUTER_ID -redis $REDIS_ADDR -rocksdb $ROCKSDB_PATH -api $API_ADDR
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "  ✓ systemd服务已创建"

# 重载systemd
sudo systemctl daemon-reload

echo ""
echo "=========================================="
echo "部署完成！"
echo "=========================================="
echo ""
echo "启动命令："
echo "  sudo systemctl start bgp-agent"
echo "  sudo systemctl enable bgp-agent  # 开机自启"
echo ""
echo "查看状态："
echo "  sudo systemctl status bgp-agent"
echo "  sudo journalctl -u bgp-agent -f"
echo ""
echo "测试命令："
echo "  curl http://localhost:9179/health"
echo "  curl http://localhost:9179/api/status"
echo ""
