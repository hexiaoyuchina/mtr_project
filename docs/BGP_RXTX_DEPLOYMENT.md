# BGP RX/TX分离架构部署指南

## 现网参数速查（Linux 200 / OP 主机）

| 项 | 值 |
|----|-----|
| SSH | `root@101.89.68.109` |
| 管理界面 | `http://101.89.68.109:8808/` |
| BGP Agent API | `http://101.89.68.109:9179` |
| `LOCAL_AS` | `63199` |
| `ROUTER_ID` | `101.89.68.109` |
| `RR_ADDR` | `139.159.43.249` |
| `RR_AS` | `63199` |
| 下游邻居（TX 通告） | `139.159.43.208` |
| `MTR_SATELLITE_PEER_IP` | `139.159.43.208`（FRR/卫星 BGP 对端，与下游一致） |

日常发版见 [部署.md](./部署.md)。

### BGP 管理页与 GoBGP 分工

| 对象 | 配置方式 |
|------|----------|
| **RR**（`139.159.43.249`） | **`bgp-agent.service`** 的 `-rr` / `-rr-as`（GoBGP **RX**），**不要**在 OP「BGP 管理」里用 FRR 卫星 VRF 再建 RR 邻居 |
| **下游**（`139.159.43.208`） | GoBGP **TX**（`POST /api/gobgp/neighbors`）或按现场仍用 FRR 卫星 VRF + 角色 `downstream` |
| 实验室 `10.133.152.*` | 仅 VM 实验；现网勿填 |

---

## 架构概述

本系统采用**GoBGP RX/TX分离架构**，实现了BGP路由的高可用性和持久化：

```
                    ┌────────────┐
                    │     RR     │
                    └─────┬──────┘
                          │ iBGP
                    Full Table
                          │
                 ┌────────▼────────┐
                 │  GoBGP-RX       │
                 │  只负责收路由    │
                 └────────┬────────┘
                          │
                    WatchEvent
                          │
                 ┌────────▼────────┐
                 │ Route Processor │
                 │ Go服务           │
                 └──────┬──────────┘
                        │
            ┌───────────┴────────────┐
            │                        │
      ┌─────▼──────┐         ┌──────▼──────┐
      │ Redis      │         │ RocksDB     │
      │ 热缓存      │         │ 持久化RIB    │
      └─────┬──────┘         └──────┬──────┘
            │                        │
            └──────────┬────────────┘
                       │
               Effective RIB
                       │
               ┌───────▼────────┐
               │ GoBGP-TX       │
               │ 只负责通告      │
               └───────┬────────┘
                       │ iBGP
                       │
                       A (下游)
```

## 核心特性

### 1. RX/TX分离
- **RX Agent**: 只从RR接收路由，不通告
- **TX Agent**: 只向下游通告路由，不接收
- **解耦优势**: RR断连时TX继续通告，实现路由冻结

### 2. 持久化存储
- **Redis**: 热缓存，快速读写
- **RocksDB**: 持久化，重启快速恢复
- **支持百万级路由**

### 3. Freeze机制
- RR down时自动freeze
- 保持当前RIB继续通告
- 不触发withdraw
- RR恢复后自动unfreeze

## 部署步骤

### 1. 安装依赖

#### Go依赖
```bash
cd service/bgp_agent
go mod download
```

#### Python依赖
```bash
cd service
pip install -r requirements.txt
```

#### 系统依赖
```bash
# Redis
apt-get install redis-server

# RocksDB（编译时需要）
apt-get install librocksdb-dev
```

### 2. 配置参数

#### GoBGP Agent配置
编辑启动参数（或使用环境变量）：

```bash
# RR配置（iBGP 全表）
export RR_ADDR="139.159.43.249"         # RR 地址
export RR_AS="63199"                    # RR 的 AS 号

# 本地配置
export LOCAL_AS="63199"                 # 本端 AS 号
export ROUTER_ID="101.89.68.109"        # BGP Router ID（现网管理地址）

# 存储配置
export REDIS_ADDR="localhost:6379"      # Redis地址
export ROCKSDB_PATH="/var/lib/bgp_agent/rocksdb"  # RocksDB路径

# API配置
export API_ADDR=":9179"                 # 管理API监听地址
```

#### Python OP配置
```bash
# GoBGP Agent地址
export GOBGP_AGENT_URL="http://127.0.0.1:9179"
```

### 3. 启动服务

#### 启动GoBGP Agent
```bash
cd service/bgp_agent

# 编译
go build -o bgp_agent

# 启动
./bgp_agent \
  -rr 139.159.43.249 \
  -rr-as 63199 \
  -local-as 63199 \
  -router-id 101.89.68.109 \
  -redis localhost:6379 \
  -rocksdb /var/lib/bgp_agent/rocksdb \
  -api :9179
```

#### 启动Python OP
```bash
cd service
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 4. 验证部署

#### 检查GoBGP Agent状态
```bash
curl http://localhost:9179/health
curl http://localhost:9179/api/status
```

#### 检查Python OP状态
```bash
curl http://localhost:8000/api/gobgp/status
```

#### 查看BGP学习路由
```bash
curl http://localhost:9179/api/routes
curl http://localhost:8000/api/gobgp/routes
```

## 使用指南

### 前端界面

访问 `http://localhost:8000` 进入管理界面：

1. **BGP管理页面**
   - 顶部显示GoBGP架构状态面板
   - RR连接状态：显示与RR的连接状态
   - 系统状态：显示是否处于frozen状态
   - 路由数量：实时显示学习到的路由数量

2. **操作按钮**
   - **详细状态**: 查看完整的系统状态JSON
   - **冻结**: 手动触发freeze（测试用）
   - **解冻**: 手动解除freeze

### API接口

#### 获取系统状态
```bash
GET /api/gobgp/status
```

#### 获取路由列表
```bash
GET /api/gobgp/routes?page=1&page_size=100
```

#### 添加下游邻居
```bash
POST /api/gobgp/neighbors
Content-Type: application/json

{
  "address": "139.159.43.208",
  "remote_as": 63199
}
```

> `remote_as` 须与对端现网配置一致；上例按同 AS iBGP 下游填写。

#### 删除下游邻居
```bash
DELETE /api/gobgp/neighbors/139.159.43.208
```

#### 手动冻结/解冻（测试用）
```bash
POST /api/gobgp/freeze
POST /api/gobgp/unfreeze
```

## 故障处理

### RR连接断开

**现象**: 前端显示"RR连接状态: ✗ 断开"

**系统行为**:
1. 自动进入freeze模式
2. 停止接受新的路由更新
3. 保持当前RIB继续向下游通告
4. 不触发withdraw

**恢复步骤**:
1. 修复RR连接
2. 系统自动检测到RR恢复
3. 自动解除freeze
4. 开始接受新的路由更新

### 系统重启

**启动流程**:
1. GoBGP Agent启动
2. 从RocksDB恢复路由到内存
3. 恢复路由到Redis热缓存
4. TX Agent重新向下游通告全部路由
5. RX Agent重新连接RR

**预期时间**:
- 百万条路由约需1-2分钟完成恢复
- 下游邻居会收到完整的路由表

### Redis故障

**影响**: 热缓存不可用，性能下降

**系统行为**:
- 继续从RocksDB读取路由
- 路由查询变慢
- 不影响路由通告

**恢复**: 重启Redis后自动恢复

### RocksDB损坏

**影响**: 持久化数据丢失

**系统行为**:
- 从RR重新学习全量路由
- 写入新的RocksDB

**预防**: 定期备份RocksDB目录

## 性能调优

### 百万级路由优化

1. **增加Redis内存**
   ```bash
   # redis.conf
   maxmemory 16gb
   ```

2. **RocksDB写入优化**
   - 批量写入（默认1000条/批）
   - 定期刷盘（默认5秒）

3. **系统资源**
   - CPU: 32核+
   - 内存: 128GB+
   - 磁盘: NVMe SSD

### 监控指标

- RR连接状态
- 路由数量
- Redis内存使用
- RocksDB磁盘空间
- 批量写入队列长度

## 与FRR架构对比

| 特性 | FRR架构 | GoBGP RX/TX分离架构 |
|------|---------|---------------------|
| RR断连处理 | 撤销所有路由 | freeze，保持通告 |
| 重启恢复 | 需要重新学习 | 从RocksDB快速恢复 |
| 路由持久化 | 无 | Redis + RocksDB |
| 百万级路由 | 性能瓶颈 | 优化支持 |
| 控制平面HA | 弱 | 强 |

## 常见问题

**Q: 如何验证freeze机制？**

A: 
1. 前端点击"冻结"按钮
2. 观察状态变为"❄️ 冻结（保持RIB）"
3. 路由数量保持不变
4. 点击"解冻"恢复

**Q: 路由数量不更新？**

A: 检查：
1. RR连接是否正常
2. 是否处于frozen状态
3. RX Agent日志

**Q: 性能优化建议？**

A:
1. 使用SSD存储RocksDB
2. 增大Redis内存配置
3. 调整批量写入大小
4. 使用更多CPU核心

## 技术支持

- 日志位置: 
  - GoBGP Agent: stdout/stderr
  - Python OP: uvicorn日志
  
- 调试工具:
  - `curl http://localhost:9179/api/status`
  - `curl http://localhost:9179/api/routes/count`
  
- 监控指标:
  - Prometheus集成（待开发）
  - 系统资源监控
