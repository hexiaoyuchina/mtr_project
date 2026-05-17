# BGP 双向中间人架构（最终版）

本文描述 **现网最终形态**：GoBGP **RX/TX 分离** + OP（FastAPI + **SQLite**）实现 **双向学 / 存 / 冻 / 搬**。控制面以 **bgp-agent** 为准，**不再以 FRR 为 BGP 会话与 RIB 源**；Web/API 展示的学习路由 **只读 SQLite**，不实时查 Agent。

网口与地址分工见 **[BGP_OP_NETWORK.md](./BGP_OP_NETWORK.md)**。表结构与 HTTP 接口见 **[BGP_DATA_AND_API.md](./BGP_DATA_AND_API.md)**。部署步骤见 **[BGP_RXTX_DEPLOYMENT.md](./BGP_RXTX_DEPLOYMENT.md)**。

---

## 1. 要解决的问题

传统单进程 BGP（含 FRR）常见行为：

```
RR 断链 → peer down → 全量 withdraw → 下游立刻丢路由
```

本系统目标：

| 场景 | 期望 |
|------|------|
| 上游 RR 断链 | **冻结** 已学路由；TX 继续向下游通告；**不**因 RR down 而撤销下游 |
| 下游运营商断链 | 冻结该 peer 在库内的 **下游窗** 快照；恢复后再覆盖 |
| 运维与交叉通告 | 以 **SQLite 定时快照** 为数据源，支持 `@upstream` / `@downstream` 全量搬运 |

---

## 2. 逻辑角色与物理路径

```
                    ┌─────────────────┐
                    │  RR（真上游）    │
                    │ 139.159.43.249  │
                    └────────┬────────┘
                             │ 上游窗 · RX · 本端 207
                             │ enp59s0f0np0
                    ┌────────▼────────┐
                    │  OP + bgp-agent │
                    │ 207 / AS 63199  │
                    │ SQLite 快照      │
                    └────────┬────────┘
                             │ 下游窗 · TX · 冒充 249 等
                             │ eno1np0 + 卫星 VRF
                    ┌────────▼────────┐
                    │  运营商 / 对端   │
                    │ 139.159.43.208  │
                    └─────────────────┘

管理面：enp59s0f1np1 → 101.89.68.109:8808（不参与 BGP 数据面）
```

- **上游窗（upstream）**：与真 RR 的会话；本端 TCP 源 **207**；Agent **RX** 收全表；写入 SQLite `route_window=upstream`（VRF 多为 `gobgp-rr`）。
- **下游窗（downstream）**：卫星 VRF（`vbgp*`）内以 **冒充 IP**（如 249）为 TCP 源连运营商；Agent **TX** 收 ADJ-IN；写入 `route_window=downstream`。

---

## 3. 软件分层

```
┌──────────────────────────────────────────────────────────────┐
│  Web UI (static/index.html)                                   │
│  · BGP 管理：邻居 / 交叉通告 / freeze 状态                       │
│  · 学习路由：上游/下游分窗 · 只读 SQLite                         │
└────────────────────────────┬─────────────────────────────────┘
                             │ HTTP :8808
┌────────────────────────────▼─────────────────────────────────┐
│  mtr-op (service/app)                                         │
│  · main.py：REST API、后台 _bgp_rib_sync_loop                   │
│  · bgp_bidirectional_sync：定时双向写库                         │
│  · bgp_learned_routes_sync：上游 RR RIB → SQLite                │
│  · bgp_control：调用 bgp-agent                                  │
│  · storage.py：SQLite 表与 freeze / 通告来源解析                  │
└────────────────────────────┬─────────────────────────────────┘
                             │ HTTP :9179
┌────────────────────────────▼─────────────────────────────────┐
│  bgp-agent (service/bgp_agent)                                │
│  RX Agent      → WatchEvent → Route Processor → Redis/RocksDB │
│  TX Agent      → 按 VRF 向下游通告；peer down 时 VRF freeze     │
│  RunPeerWatch  → RR/下游 Established 变化 → Freeze/Unfreeze   │
└──────────────────────────────────────────────────────────────┘
```

**数据权威分工**

| 数据 | 运行时真相 | OP 展示 / 交叉通告 |
|------|------------|-------------------|
| 有效 RIB（上游） | Agent Processor + Redis/RocksDB | 定时同步 → `bgp_learned_routes` |
| 下游 ADJ-IN | TX 池内存 | 定时 `GET /api/tx/learned-routes` → SQLite |
| 邻居元数据、通告开关 | SQLite `bgp_neighbor_meta` | BGP 管理页 |
| Freeze 位（库侧） | SQLite `bgp_peer_snapshot` | 学习路由页、同步时跳过覆盖 |

Agent 内 **Redis + RocksDB** 用于百万级 RIB 与重启恢复；**业务界面与「路由通告」不以 Agent 实时 RIB 为准**，以 SQLite 快照为准。

---

## 4. 双向：学 / 存 / 冻 / 搬

### 4.1 学（Learn）

| 方向 | Agent 来源 | OP 同步模块 |
|------|------------|-------------|
| 上游 | `GET /api/routes`（RX 有效 RIB） | `bgp_learned_routes_sync.sync_bgp_learned_routes` |
| 下游 | `GET /api/tx/learned-routes?vrf=` | `bgp_bidirectional_sync.sync_downstream_routes_for_vrf` |

后台任务：`main._bgp_rib_sync_loop` → `sync_bidirectional_routes`（周期 **`MTR_BGP_RIB_SYNC_SEC`**，默认 60；**`MTR_BGP_RIB_SYNC=0`** 可关）。

### 4.2 存（Store）

- 按 **peer** 覆盖写入 `bgp_learned_routes`（`replace_bgp_learned_routes_for_peer`）。
- 更新 `bgp_peer_snapshot`（条数、`last_sync_at`、`window_type`）。
- 全局同步结果写入 `bgp_rib_sync_state`。
- 上游前缀可选写入 `bgp_upstream_route_cache`（断链后合并展示 stale，见 `merge_upstream_stale`）。

### 4.3 冻（Freeze）

两层配合：

1. **Agent**（`RunPeerWatch`，默认 15s，`MTR_BGP_PEER_WATCH_SEC`）  
   - RR 非 Established → Processor 停收更新 + `txPool.FreezeAll()`  
   - 某下游 VRF peer 非 Established → 该 VRF TX freeze，继续通告已有路由  

2. **OP SQLite**（`bgp_peer_snapshot.frozen=1`）  
   - 定时同步 **不覆盖** 该 peer 的 `bgp_learned_routes`  
   - 学习路由 API 返回 `peer_frozen=true`  

RR 删除时 Agent 侧 `FreezeAll`；OP 删邻居会清该 IP 相关学习行。

### 4.4 搬（Cross-advertise）

在 **BGP 管理** 行内「路由通告」：

| 本行邻居角色 | 默认通告来源 `advertise_routes_from` | 含义 |
|--------------|--------------------------------------|------|
| RR | `@downstream` | 把 **下游窗** 学到的前缀通告给 RR（经 `POST /api/rr/routes`） |
| 下游 | `@upstream` | 把 **上游窗** 学到的前缀通告给该下游（经 `POST /api/tx/routes`） |

来源还可填 **具体邻居 IP**，或 UI datalist 中的 `@upstream` / `@downstream`。解析见 `storage.iter_bgp_routes_for_advertise_source`。

新建邻居时若未填来源，`_default_advertise_routes_from` 写入 meta 默认值。

---

## 5. RX/TX 分离（Agent 内）

```
     RR ──iBGP──► RX Agent ──Watch──► Processor ──► Redis / RocksDB
                                              │
                                              ▼
                                        Effective RIB
                                              │
     下游 ◄──iBGP── TX Agent ◄────────────────┘
```

**为何分离**：同一进程里 peer down 往往伴随 withdraw；RX 只收、TX 只发，才能在 RR down 时 **freeze 当前 RIB** 且 TX **继续通告**。

关键代码：

| 模块 | 路径 |
|------|------|
| RX | `service/bgp_agent/pkg/rx/` |
| TX | `service/bgp_agent/pkg/tx/` |
| Processor / Freeze | `service/bgp_agent/pkg/processor/` |
| Peer Watch | `service/bgp_agent/api_bidirectional.go` |
| HTTP | `service/bgp_agent/api_server.go` |

---

## 6. 与 MTR / 逐跳替换的关系

BGP 中间人负责 **控制面路由学习与交叉通告**；转发面 ICMP/MTR **逐跳源地址替换** 由 `hop_replace_rules` + `te_rewrite_nfqueue`（及 nft）完成，二者正交。部署时注意 `MTR_TE_REWRITE_SCRIPT` 指向实际脚本路径（见 [部署.md](./部署.md)）。

---

## 7. 现网参数速查

| 项 | 值 |
|----|-----|
| 管理 IP | `101.89.68.109`（`enp59s0f1np1`） |
| Web | `http://101.89.68.109:8808/` |
| bgp-agent API | `http://127.0.0.1:9179` |
| `LOCAL_AS` | `63199` |
| RR | `139.159.43.249` |
| RX 本端 / Router ID | `139.159.43.207`（`enp59s0f0np0`） |
| 下游示例 | `139.159.43.208`（卫星 VRF，`eno1np0`） |

---

## 8. 关联文档

| 文档 | 内容 |
|------|------|
| [BGP_DATA_AND_API.md](./BGP_DATA_AND_API.md) | SQLite 表字段、OP / Agent HTTP 接口 |
| [BGP_OP_NETWORK.md](./BGP_OP_NETWORK.md) | 三网口分工与环境变量 |
| [BGP_RXTX_DEPLOYMENT.md](./BGP_RXTX_DEPLOYMENT.md) | 编译、systemd、验收 |
| [BGP_ARP_SPOOF_MULTI_SESSION.md](./BGP_ARP_SPOOF_MULTI_SESSION.md) | ARP + 多 VRF 冒充（内核侧） |
| [部署.md](./部署.md) | 日常发版 |

实验室 `10.133.152.*` 拓扑见 [bgp-ipvlan-setup.md](./bgp-ipvlan-setup.md)，**勿直接套用到现网**。
