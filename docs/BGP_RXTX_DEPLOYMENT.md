# BGP 部署与验收

架构与表/API 说明见 **[BGP_ARCHITECTURE.md](./BGP_ARCHITECTURE.md)**、**[BGP_DATA_AND_API.md](./BGP_DATA_AND_API.md)**。网口勿配错见 **[BGP_OP_NETWORK.md](./BGP_OP_NETWORK.md)**。日常发版 **[部署.md](./部署.md)**。

---

## 1. 现网参数速查

| 项 | 值 |
|----|-----|
| SSH / Web | `101.89.68.109:8808`（`enp59s0f1np1`） |
| bgp-agent | `http://127.0.0.1:9179` |
| `LOCAL_AS` / `RR_AS` | `63199` |
| `ROUTER_ID` | `139.159.43.207`（RX TCP 源，`enp59s0f0np0`） |
| RR | `139.159.43.249` |
| 下游 | `139.159.43.208`（卫星 VRF，`eno1np0`） |
| `MTR_BGP_IPVLAN_BASE_IFACE` | **`eno1np0`** |
| `MTR_BGP_RR_UPLINK_IFACE` | **`enp59s0f0np0`** |

---

## 2. 组件与分工

| 组件 | 单元 | 职责 |
|------|------|------|
| **bgp-agent** | `bgp-agent.service` | GoBGP RX/TX、Freeze、Agent API |
| **mtr-op** | `mtr-op.service` | Web/API、SQLite、定时双向同步、交叉通告 |

| 邻居类型 | 配置入口 | Agent 动作 |
|----------|----------|------------|
| RR `249` | Web「BGP 管理」→ 角色 **RR** | `POST /api/rr/config` |
| 下游 `208` | 角色 **下游** + 卫星 VRF | TX 按 VRF 懒启动，`/api/neighbors/add` |

RR **不要**写进 `bgp-agent` 启动参数 `-rr`；由 OP 创建。

---

## 3. 依赖与编译

```bash
# Agent（目标机需 Go 工具链）
cd service/bgp_agent
go mod download
go build -o /usr/local/bin/bgp_agent .

# OP
cd service
pip install -r requirements.txt

# Agent 运行时依赖
apt-get install -y redis-server librocksdb-dev
```

仓库脚本：`service/scripts/deploy_bgp_rxtx.py`（上传、systemd、环境变量模板）。

---

## 4. 环境变量（要点）

### bgp-agent（`/var/lib/bgp_agent/bgp-agent.env` 或 systemd）

```bash
LOCAL_AS=63199
ROUTER_ID=139.159.43.207
REDIS_ADDR=localhost:6379
ROCKSDB_PATH=/var/lib/bgp_agent/rocksdb
API_ADDR=:9179
MTR_BGP_PEER_WATCH_SEC=15
```

### mtr-op（`service/systemd/mtr-op.service`）

```bash
GOBGP_AGENT_URL=http://127.0.0.1:9179
MTR_BGP_RIB_SYNC=1
MTR_BGP_RIB_SYNC_SEC=60
MTR_BGP_IPVLAN_AUTO=1
MTR_BGP_SAT_DNAT_AUTO=1
MTR_BGP_IPVLAN_BASE_IFACE=eno1np0
MTR_BGP_RR_UPLINK_IFACE=enp59s0f0np0
MTR_BGP_RR_SPOOF_IPVLAN_ADDR=1
MTR_BGP_IPVLAN_PEER_IP=139.159.43.208
MTR_SATELLITE_PEER_IP=139.159.43.208
MTR_SATELLITE_BGP_TCP_SOURCE=spoof
```

---

## 5. 启动顺序

```bash
systemctl enable --now redis-server
systemctl enable --now bgp-agent
systemctl enable --now mtr-op
```

首次现网建议顺序（冒充 RR 连下游）见 [BGP_OP_NETWORK.md](./BGP_OP_NETWORK.md)「操作顺序」。

---

## 6. 验收清单

### 6.1 Agent 存活

```bash
curl -sf http://127.0.0.1:9179/health
curl -s http://127.0.0.1:9179/api/status | head -c 800
curl -s http://127.0.0.1:9179/api/peers/freeze-status
```

### 6.2 OP 与后台同步

```bash
curl -s http://127.0.0.1:8808/api/gobgp/status
curl -s "http://127.0.0.1:8808/api/bgp/learned-routes/filter-options"
curl -s "http://127.0.0.1:8808/api/bgp/learned-routes?route_window=upstream&page_size=5"
curl -s "http://127.0.0.1:8808/api/bgp/learned-routes?route_window=downstream&page_size=5"
```

Web：**学习路由** 页应显示上游/下游 KPI；**立即同步** 后 `last_sync_ok` 为成功。

### 6.3 双向同步日志

```bash
journalctl -u mtr-op -f | grep -i bidirectional
```

期望周期性 `bidirectional sync done`；RR 断链时见 `freeze` 且库内条数不删。

### 6.4 交叉通告

BGP 管理行：RR 默认来源 `@downstream`，下游默认 `@upstream`；点「应用」后查 `journalctl -u mtr-op` 与 Agent 日志。

### 6.5 卫星策略路由与 DNAT（冒充 IP 连下游）

收敛后核对（示例 249 / 208）：

```bash
nft list chain inet mtr_bgp_sat_dnat prerouting | grep 139.159.43.249
ip -4 rule show | grep 139.159.43.249
ip route get 139.159.43.208 from 139.159.43.249   # 期望 dev iv249、卫星表
ss -tlnp | grep ':1830'                          # TX 监听（vbgp13915943249）
```

缺规则时：`python 109/reconcile_satellite.py` 或 `POST /api/arp-spoof/satellite-vrfs/reconcile`。  
说明见 [BGP_SATELLITE_IP_RULE_AND_DNAT.md](./BGP_SATELLITE_IP_RULE_AND_DNAT.md)。

---

## 7. 故障处理（简表）

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| 学习路由为空 | 未同步 / Agent down | `POST /api/bgp/learned-routes/sync`；查 `bgp-agent` |
| 上游有、下游无 | TX 未 Established / VRF 错 | `peers/freeze-status`；核对 `eno1np0` 与卫星 VRF |
| RR 断链后下游仍通 | 设计行为（Freeze） | Agent TX freeze + SQLite 保留快照 |
| 学习页与 Agent 条数不一致 | 页面只读 SQLite | 等同步或手动同步；勿用 `/api/gobgp/routes` 对比 UI |
| ipvlan 与 RR 争用同一口 | `MTR_BGP_IPVLAN_BASE_IFACE` 配错 | 必须为 `eno1np0`，见 BGP_OP_NETWORK |
| 下游 Active；日志 `passive connection` 对端 IP | 缺 **nft DNAT** 或 **ip rule** | 补跑卫星收敛；见 BGP_SATELLITE_IP_RULE_AND_DNAT |
| `from 冒充IP` 走 enp59 | 无 `ip rule` | `reconcile_satellite.py` / satellite-vrfs reconcile |

Agent 内 Redis/RocksDB 故障：RIB 仍可从 RocksDB 恢复；OP SQLite 需同步成功才有 Web 数据。

---

## 8. 发版注意

1. 修改 `service/bgp_agent` 后 **必须** `go build` 并 `systemctl restart bgp-agent`。  
2. 修改 `service/app` 后 `systemctl restart mtr-op`。  
3. 修改 `service/static/index.html` 后重启 mtr-op 或清浏览器缓存。  
4. Schema 变更随 `storage.init_schema` 迁移列，无需手改库（新库自动建表）。
