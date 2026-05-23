# BGP 数据模型与 HTTP 接口

与架构说明配套：[BGP_ARCHITECTURE.md](./BGP_ARCHITECTURE.md)（现网）、**目标态** [BGP_FIB_TARGET.md](./BGP_FIB_TARGET.md)（FIB、自动入库/通告、**去掉入库·通告开关**）。

现网：**§2** 百万 RIB = Agent **Redis/RocksDB** 按 peer；SQLite = 邻居 meta / freeze；学习路由 **读 Agent**。

> **目标态变更摘要**见 [BGP_FIB_TARGET.md §13](./BGP_FIB_TARGET.md#13-现网文档api-目标态变更摘要)：`store_received_routes` / `advertise_routes` 及对应 API、UI 按钮 **将删除**。

默认 OP 库文件由 `MTR_DB` 指定（常见 `/root/mtr_op/data.db`），schema 在 `service/app/storage.py` 的 `init_schema` 中初始化。Agent 侧 RIB 见 `GET /api/routes`、`GET /api/storage/stats`（`:9179`）。

---

## 1. SQLite 表一览（OP 控制面，非百万 RIB 主库）

| 表名 | 用途 |
|------|------|
| `bgp_neighbor_meta` | 邻居角色、TCP 源、交叉通告开关与来源 |
| `bgp_learned_routes` | **双向学习路由快照**（Web「学习路由」唯一数据源） |
| `bgp_peer_snapshot` | 每 peer 的窗类型、freeze、条数、最近同步时间 |
| `bgp_rib_sync_state` | 全局最近一次双向同步结果（单行 `id=1`） |
| `bgp_upstream_route_cache` | 上游前缀持久缓存（RR 断链后 stale 合并展示） |
| `bgp_sticky_frr` | Sticky 下游通告安装记录（可选内核协调） |

---

## 2. 表字段说明

### 2.1 `bgp_neighbor_meta`

| 字段 | 类型 | 说明 |
|------|------|------|
| `vrf` | TEXT PK | VRF 名；RR 多为 `gobgp-rr` 或 `default`；下游为 `vbgp*` |
| `neighbor_ip` | TEXT PK | 对端 BGP 邻居地址 |
| `role` | TEXT | `rr` / `upstream` / `downstream` / `unknown` |
| `note` | TEXT | 备注 |
| `source_ip` | TEXT | 本端 TCP 源（update-source）；RR 为 207，下游常为冒充 IP |
| `advertise_routes` | INTEGER | `1` 启用行内交叉通告 |
| `advertise_routes_from` | TEXT | 遗留字段；新 UI 通告仅读 Agent 按 peer RIB |
| `store_received_routes` | INTEGER | `1` 将从对端收到的路由写入 Agent Redis/RocksDB |
| `created_at` | TEXT | 创建时间 UTC |

**用途**：BGP 管理页展示；**路由入库** / **通告缓存** 开关；`routes_cached` 由 OP 调 Agent `GET /api/rib/routes/count` 填充。

---

### 2.2 `bgp_learned_routes`

| 字段 | 类型 | 说明 |
|------|------|------|
| `vrf` | TEXT PK | 路由所属 VRF |
| `prefix` | TEXT PK | IPv4 前缀，如 `1.2.3.0/24` |
| `nexthop` | TEXT PK | 下一跳 |
| `neighbor_ip` | TEXT | 来源邻居（含 RR 或下游对端） |
| `remote_as` | INTEGER | 远端 AS |
| `role` | TEXT | 同步时推断：`rr` / `upstream` / `downstream` |
| `as_path` | TEXT | AS_PATH 字符串 |
| `updated_at` | TEXT | 本条写入时间 UTC |
| `route_window` | TEXT | **`upstream`**：RR/上游窗；**`downstream`**：下游 ADJ-IN 窗 |

**用途**：

- `GET /api/bgp/learned-routes` 分页列表（可按 `route_window`、`vrf`、`neighbor_ip` 筛选）。
- `iter_bgp_routes_for_advertise_source`：交叉通告批量读库。
- 按 peer 全量替换：`replace_bgp_learned_routes_for_peer`（freeze 的 peer 跳过）。

**索引**：`vrf`、`prefix`、`neighbor_ip`、`(vrf, neighbor_ip)`。

---

### 2.3 `bgp_peer_snapshot`

| 字段 | 类型 | 说明 |
|------|------|------|
| `vrf` | TEXT PK | 邻居 VRF |
| `neighbor_ip` | TEXT PK | 邻居 IP |
| `window_type` | TEXT | `upstream` 或 `downstream` |
| `frozen` | INTEGER | `1` 表示库内快照保护中，定时同步不覆盖该 peer 路由 |
| `session_established` | INTEGER | 最近一次同步时会话是否 Established |
| `route_count` | INTEGER | 库内该 peer 路由条数（或 freeze 时 Agent 侧条数缓存） |
| `last_sync_at` | TEXT | 最近同步时间 |

**用途**：学习路由页「Peer 快照 / freeze」；`is_bgp_peer_frozen`；与 Agent `GET /api/peers/freeze-status` 逻辑对齐。

---

### 2.4 `bgp_rib_sync_state`

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER | 固定为 `1` |
| `last_sync_at` | TEXT | 最近一次 `sync_bidirectional_routes` 完成时间 |
| `last_ok` | INTEGER | `1` 成功 / `0` 失败 |
| `last_error` | TEXT | 失败原因摘要 |

**用途**：学习路由页顶部「上次同步」；`data_source=rib_sqlite_sync_failed` 判定。

---

### 2.5 `bgp_upstream_route_cache`

| 字段 | 类型 | 说明 |
|------|------|------|
| `learn_vrf` | TEXT PK | 学习 VRF（默认 `gobgp-rr`，`MTR_BGP_UPSTREAM_CACHE_VRF` 可改） |
| `prefix` | TEXT PK | 前缀 |
| `nexthop` | TEXT | 下一跳 |
| `neighbor_ip` | TEXT | 来源邻居 |
| `remote_as` | INTEGER | AS |
| `as_path` | TEXT | AS_PATH |
| `last_live_at` | TEXT | 上次在 live RIB 中出现时间 |

**用途**：上游 RR 断链后，当前 RIB 快照中已缺失的前缀仍可合并进学习列表（`stale=true`，`data_source=upstream_cache_sqlite`）。

---

### 2.6 `bgp_sticky_frr`

| 字段 | 类型 | 说明 |
|------|------|------|
| `advert_vrf` | TEXT PK | 通告目标 VRF |
| `prefix` | TEXT PK | 已 sticky 安装前缀 |
| `installed_at` | TEXT | 安装时间 |

**用途**：`MTR_BGP_STICKY_ADVERT` 相关下游协调（见 `bgp_sticky_reconcile.py`）。

---

## 3. 路由窗（route_window）判定

查询时若 `route_window` 列为空，按 `role` 回退：

- `downstream` 角色 → 下游窗  
- 其余 → 上游窗  

`summarize_learned_routes_by_window` 用于 KPI：上游条数 / 下游条数 / 合计。

---

## 4. mtr-op API（`:8808`）

### 4.1 BGP 邻居与实例

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/bgp/neighbors` | 合并 Agent 会话 + SQLite meta（含默认 `@upstream`/`@downstream`） |
| POST | `/api/bgp/neighbors` | 新增；RR → `POST` Agent `/api/rr/config`；下游 → TX 加邻居 |
| PATCH | `/api/bgp/neighbors/{vrf}/{neighbor_ip}` | 改 IP/AS/角色/源地址（RR 会先删后建） |
| DELETE | `/api/bgp/neighbors/{vrf}/{neighbor_ip}` | 删除；RR 调 Agent remove；清该 IP 学习路由 |
| POST | `/api/bgp/neighbors/{vrf}/{neighbor_ip}/toggle` | 启停邻居 |
| POST | `/api/bgp/neighbors/{vrf}/{neighbor_ip}/store-routes` | **路由入库**：对端通告给本机的路由写入 Agent；开启时自动 `ingest-peer`（RR/下游统一） |
| POST | `/api/bgp/neighbors/{vrf}/{neighbor_ip}/advertise` | **路由通告**开关：触发 Agent 流式任务（body: `advertise_routes`）；**不**走分页 `GET /api/rib/routes` |
| GET | `/api/bgp/neighbors/{vrf}/{neighbor_ip}/advertise/status` | 异步通告任务进度 |
| GET | `/api/bgp/vrfs` | VRF 列表（内核 + Agent + `gobgp-rr`） |
| GET | `/api/bgp/satellite-vrfs` | ARP 表中的卫星 VRF |
| GET | `/api/bgp/neighbor-form-hints` | 表单提示（含 `@upstream`、`@downstream`） |
| POST | `/api/bgp/instances` | 仅建内核/元数据（少用） |
| POST | `/api/bgp/sync-from-frr` | 兼容 URL：从 **bgp-agent** 合并邻居到 SQLite meta（非学习路由主路径；**不是** vtysh/FRR） |

**`BgpNeighborOut` 扩展字段**：`store_received_routes`、`routes_cached`（Agent 持久库条数）、`routes_received`（会话 `pfx_rcd`）。

---

### 4.2 学习路由（Agent 持久库，OP 分页代理）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/bgp/learned-routes` | **须** `vrf` + `neighbor_ip` 才返回明细；否则仅 `summary` 汇总各 peer 条数 |
| GET | `/api/bgp/learned-routes/filter-options` | Agent 邻居 VRF/IP + RIB 汇总 |
| POST | `/api/bgp/learned-routes/ingest` | 从对端 ADJ-RIB-In 全量灌库（Query: `vrf`, `neighbor_ip`；RR/下游统一） |
| POST | `/api/bgp/learned-routes/sync` | 兼容：带 `vrf`+`neighbor_ip` 时等同 ingest |

**单条路由 `BgpLearnedRouteOut`**：`data_source=rib_agent`；`route_window`、`peer_frozen` 仍来自 SQLite meta。

**`bgp_learned_routes` 表**：保留用于历史/后台任务，**不再是 Web 学习路由主数据源**。

---

### 4.3 GoBGP 代理（兼容/调试，优先用 `/api/bgp/*`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/gobgp/status` | 转发 Agent `/api/status` + RR 状态 |
| GET | `/api/gobgp/routes` | Agent 有效 RIB（**非**学习页数据源） |
| GET | `/api/gobgp/routes/count` | RIB 条数 |
| POST | `/api/gobgp/neighbors` | 直加 TX 邻居（绕过 OP meta 时用） |
| DELETE | `/api/gobgp/neighbors/{address}` | 删邻居 |
| POST | `/api/gobgp/freeze` | 手动冻 RR（测试） |
| POST | `/api/gobgp/unfreeze` | 手动解冻 |

---

## 5. bgp-agent API（`:9179`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/api/status` | RX/TX/Processor 摘要 |
| GET | `/api/routes` | RX 有效 RIB（OP 上游同步源） |
| GET | `/api/routes/count` | RIB 条数 |
| GET | `/api/tx/learned-routes?vrf=` | 指定 VRF 下游 ADJ-IN（OP 下游同步源） |
| GET | `/api/peers/freeze-status` | 上游 RR + 各下游 peer 的 established/frozen |
| POST | `/api/rr/config` | 配置 RR 邻居（OP 新增 RR 时调用） |
| POST | `/api/rr/remove` | 移除 RR |
| GET | `/api/rr/status` | RR 会话状态 |
| POST | `/api/rr/freeze` | 手动 freeze RR 路径 |
| POST | `/api/rr/unfreeze` | 解冻 |
| POST | `/api/rr/routes` | 向 RR 通告/撤销前缀（OP 交叉通告 → RR） |
| POST | `/api/tx/routes` | 向下游 VRF 通告/撤销（OP 交叉通告 → 下游） |
| GET | `/api/neighbors` | 所有已配置邻居 |
| POST | `/api/neighbors/add` | 添加 TX 邻居 |
| POST | `/api/neighbors/remove` | 删除邻居 |
| POST | `/api/neighbors/toggle` | 启停 |
| GET | `/api/storage/stats` | Redis/RocksDB 统计 |
| GET | `/api/rib/routes` | 按 peer 分页（**仅 Web/查询**）：`page`、`page_size` |
| GET | `/api/rib/routes/count` | 按 peer 条数 O(1)（UI 与通告进度分母） |
| POST | `/api/rib/advertise` | 流式通告任务：`IteratePeerRoutes` → TX/RR 批量 AddPath |
| POST | `/api/rib/withdraw` | 流式撤销任务（同上，`enable=false`） |
| GET | `/api/rib/advertise/status` | 任务进度：`task_id` |
| GET/POST | `/api/rib/policy` | 读/写 `store_routes` 等 per-peer 策略 |
| POST | `/api/rib/ingest-peer` | 对端 Adj-RIB-In → 按 peer 持久库（Query: `window`, `vrf`, `neighbor_ip`） |
| POST | `/api/rib/ingest-downstream` | 兼容别名（等同 `ingest-peer`，`window=downstream`） |

---

## 6. 关键环境变量（OP）

| 变量 | 默认 | 用途 |
|------|------|------|
| `MTR_BGP_RIB_SYNC` | `1` | `0` 关闭后台双向同步 |
| `MTR_BGP_RIB_SYNC_SEC` | `60` | 同步周期（秒） |
| `MTR_BGP_UPSTREAM_CACHE_VRF` | `gobgp-rr` | 上游学习 VRF / stale 合并范围 |
| `MTR_BGP_IPVLAN_BASE_IFACE` | `eno1np0` | 下游 ipvlan 父口 |
| `MTR_BGP_RR_UPLINK_IFACE` | `enp59s0f0np0` | 真 RR 二层口 |
| `MTR_BGP_RR_SPOOF_IPVLAN_ADDR` | `0` | `1` 时上下联隔离：在下游 `iv*` 挂 RR `/32`，删主表 `249→上联` |
| `MTR_BGP_IPVLAN_PEER_IP` | 现网 208 | 下游对端地址 |
| `MTR_BGP_ROLE_MAP` | — | `ip:role` 逗号分隔默认角色 |
| `MTR_BGP_DB_PRESETS` | — | `vrf:ip:role` 预设写入 meta |
| `GOBGP_AGENT_URL` | `http://127.0.0.1:9179` | Agent 基址 |

Agent：`MTR_BGP_PEER_WATCH_SEC`（默认 15）控制 `RunPeerWatch` 周期。

---

## 7. 交叉通告数据流

```
SQLite bgp_learned_routes
        │
        ├─ @upstream  ──► 过滤 route_window=upstream
        ├─ @downstream ──► 过滤 route_window=downstream
        └─ <neighbor_ip> ──► 按 neighbor_ip 精确
        │
        ▼
_async_apply_bgp_route_advertise
        │
        ├─ 目标 RR  ──► POST /api/rr/routes  (enable=true)
        └─ 目标下游 ──► POST /api/tx/routes   (vrf + routes[])
```

任务状态键：`{vrf}-{neighbor_ip}-advertise`，查询 `GET .../advertise/status`。
