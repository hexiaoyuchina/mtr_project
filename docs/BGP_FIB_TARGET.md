# BGP RIB / FIB 目标架构（定稿汇总）

本文描述 **产品目标形态**。现网行为见 [BGP_ARCHITECTURE.md](./BGP_ARCHITECTURE.md)；表与 HTTP 见 [BGP_DATA_AND_API.md](./BGP_DATA_AND_API.md)（其中 **入库/通告开关为现网**，目标态见本文 §5、§13）。

**IP 均为举例**；实现须从 **`bgp_neighbor_meta` 等 SQLite 配置读邻居**，禁止写死 245/247/208/249。

---

## 1. 拓扑与两扇窗

### 1.1 上游窗（207 ↔ 多个真 RR）

- 服务以 **207**（及库中配置的上联本端源）与 **多个 Route Reflector / 上游 RR** 建立 iBGP（同 AS）。
- **学习**各 RR 通告的百万级前缀 → 写入 **upstream RIB（按 RR peer 分存）**。
- 融合为 **upstream_fib** → 向 **下游运营商** 通告 + **去上游方向内核转发**。

### 1.2 下游窗（卫星 VRF 内伪造 RR 身份 ↔ 多个下游）

- 服务在 **卫星 VRF** 内，以 **多个伪造 RR 身份**（`bgp_neighbor_meta.source_ip`）与 **多个下游设备** 建 BGP。
- **关系不是 1:1**：
  - **一个伪造身份** 可连 **多个下游**（例：身份 249 → 208、249 → 210）。
  - **一个下游** 可接 **多个伪造身份**（例：208 ← 245、208 ← 247、208 ← 249）。
- **学习**各下游通告的路由 → **downstream RIB（按 peer 分存）**。
- 融合为 **downstream_fib** → 向 **所有真 RR** 通告 + **去下游方向内核转发**。

```text
  真 RR-1 ──┐
  真 RR-2 ──┼──► [upstream RIB / peer] ──► upstream_fib ──┬──► 内核（去上游）
  真 RR-N ──┘                                              ├──► 245→208 通告（同一张 FIB）
                                                           ├──► 247→208 通告
                                                           └──► 249→210 …

  下游 208/210… ──► [downstream RIB / peer] ──► downstream_fib ──┬──► 内核（去下游）
                                                                 └──► 通告所有真 RR
```

---

## 2. 持久化数据（四类，均在 Agent，百万级）

| 名称 | 粒度 | 持久化 | 用途 |
|------|------|--------|------|
| **upstream RIB** | 每个 **真 RR peer** 一条路由集 | Redis + RocksDB | Web「BGP 路由」**按 RR 查询** |
| **downstream RIB** | 每个 **下游 peer**（键含 `vrf + neighbor_ip + source_ip`） | Redis + RocksDB | Web **按下游 / 身份查询** |
| **upstream_fib** | 每个 **prefix 一条 best**（全 RR 融合） | Redis + RocksDB | ① 所有 **已启用** 下游会话 **共用同一张表** 通告 ② **上游转发 FIB** |
| **downstream_fib** | 每个 **prefix 一条 best**（全下游融合） | Redis + RocksDB | ① 通告 **所有已启用真 RR** ② **下游转发 FIB** |

### 2.1 Key 示意（逻辑，非最终实现）

```text
rib:upstream:{vrf}:{rr_neighbor}:{prefix}
rib:downstream:{vrf}:{downstream_neighbor}:{source_ip}:{prefix}

fib:upstream:{prefix}
fib:downstream:{prefix}
```

### 2.2 SQLite（OP）只存什么

| 表 / 字段 | 内容 |
|-----------|------|
| **`bgp_neighbor_meta`** | `vrf`、`neighbor_ip`、`role`、`source_ip`、`note`、`created_at` |
| **`enabled`（或等价）** | **唯一运维开关**：是否参与 BGP 会话、RIB 刷新、FIB 通告（见 §5） |
| **废弃列** | `store_received_routes`、`advertise_routes`、`advertise_routes_from` — **目标态删除**，不再暴露 UI/API |

**不存** 百万 RIB/FIB：`bgp_learned_routes`、`bgp_upstream_route_cache`、`bgp_peer_snapshot`、Sticky 等 **目标态删除**。

---

## 3. RIB 入库（自动，无「入库」按钮）

### 3.1 触发

| 条件 | 行为 |
|------|------|
| **新增邻居且 enabled** | 建 BGP 会话；该 peer RIB **为空** → **全量灌库** |
| **已有 RIB** | **增量**：UPDATE → upsert；对端 **withdraw**（会话正常）→ 删 **该 peer 下** 该 prefix |
| **Established** | Watch ADJ-IN **持续写入**（无需 `store_received_routes` 开关） |
| **邻居 enabled=false** | **不** 建新 UPDATE 入库；**不 purge** RIB（除非删邻居） |

### 3.2 断链 vs 删除（RIB 层）

| 事件 | RIB |
|------|-----|
| **会话断链** | **不删除** 该 peer 的 RIB；断链期间 **忽略** 因本地 teardown 产生的 bulk withdraw |
| **BGP 管理删除邻居** | **purge 该 peer 全部 RIB** |

---

## 4. FIB 融合与选路参与规则

对 **每个 prefix**，从候选 RIB 路由集中 **BGP 选路**得到一条 best，写入对应 FIB。

### 4.1 谁进入候选集

| 事件 | upstream_fib 候选 | downstream_fib 候选 |
|------|-------------------|---------------------|
| **RR 会话断链** | **包含** 该 RR 的 RIB 条目 | — |
| **下游会话断链** | — | **不包含** 该 peer（不可达排除） |
| **管理删除 RR 邻居** | **不包含**（RIB 已 purge） | — |
| **管理删除下游邻居** | — | **不包含**（RIB 已 purge） |
| **RR / 下游恢复 Established** | 刷新该 peer RIB 后 **包含** | 可达后 **包含** |
| **在线 UPDATE/withdraw** | 更新 RIB → **增量重算** 受影响 prefix | 同上 |

**记忆：**

- **RR 断链**：库留着，**upstream_fib 仍用该 RR 路由选路**。
- **下游断链**：库留着，**downstream_fib 不用该 peer 选路**。
- **管理删除**：**purge RIB → 重算 FIB，该 peer 必须排除**。

### 4.2 重算与导出

- 单 prefix RIB 变更 → 只重算该 prefix（debounce 批量）。
- peer 删除 / 恢复 / 全量 ingest 完成 → 批量重算相关 prefix。
- FIB 变更 → 对 **所有 enabled 会话** 做 BGP **diff** + 内核 FIB **diff**。

---

## 5. BGP 管理界面（目标态）

### 5.1 仅保留的操作

| 操作 | 行为 |
|------|------|
| **新增邻居** | 写 `bgp_neighbor_meta` → Agent 建会话（**默认 enabled**）→ RIB 空则灌库 → 参与 FIB |
| **启用邻居** | Admin Up / 建连 → 刷新该 peer RIB → 重算 FIB → **diff 通告** |
| **停用邻居** | Admin Down / 断会话 → **不 purge RIB**；**不再** 向该 peer diff 新 FIB（可选 withdraw 已通告前缀） |
| **删除邻居** | purge RIB → 删 meta → 重算 FIB → withdraw + 撤内核 |

### 5.2 去掉的操作（现网有，目标态无）

- **「路由入库」** 开关（`store_received_routes`）  
- **「路由通告」** 开关（`advertise_routes`）  
- 相关 API：`POST .../store-routes`、`POST .../advertise`（及 advertise status 作手动任务）  
- **无** 环境变量「保险丝」（不设 `MTR_BGP_AUTO_*=0` 类总开关替代 UI）

**原则**：入库与随 FIB 通告是 **邻居 enabled 的默认能力**，不由运维逐 peer 点开。

### 5.3 通告数据流（自动）

**向下游**

- 数据源：**唯一 `upstream_fib`**。
- 目标：**每条 enabled 的 downstream 会话**（`bgp_neighbor_meta`：`role=downstream`，含 `vrf`、`neighbor_ip`、`source_ip`）。
- 245→208、247→208 等：**同一张 FIB**，不同 TCP 源 / 会话。

**向真 RR**

- 数据源：**`downstream_fib`**。
- 目标：**所有 enabled 的上游 RR**（RX AddPath/Withdraw diff）。

---

## 6. 内核转发 FIB

| FIB | 安装域 |
|-----|--------|
| **upstream_fib** | 去 **上游 / 经 RR** 方向 |
| **downstream_fib** | 去 **下游** 方向 |

**通告与转发同源**：FIB 变更 → BGP diff + `ip route replace` diff。

---

## 7. 管理删除邻居（定稿流程）

```text
1. 删除 / AdminDown Agent BGP 会话
2. purge 该 peer 在对应 window 下的全部 RIB
3. DELETE bgp_neighbor_meta 行
4. 重算 FIB（删 RR → upstream_fib；删下游 → downstream_fib）
5. BGP withdraw + 内核撤路由 diff
```

---

## 8. 会话恢复与在线更新

| 事件 | 动作 |
|------|------|
| **Established（enabled）** | RIB 空 → 全量灌库；有 → 与 ADJ-IN 对齐增量 |
| **UPDATE / withdraw（会话正常）** | 更新 peer RIB → 重算 FIB → diff |
| **FIB 变化** | 所有 **enabled** 下游会话 + 所有 **enabled** RR + 内核 |

---

## 9. 部署、重启与性能

重启/发版 **不得** 依赖关闭「入库/通告」开关来降压；约定如下：

| 阶段 | 行为 |
|------|------|
| **Agent 启动** | 从 RocksDB **加载 RIB + FIB**；**不** 对全网 re-ingest |
| **OP / Agent 重启** | `deploy_light.py` **保留** `/var/lib/bgp_agent/rocksdb` 与 `data.db` |
| **邻居 Established** | 仅 **该 peer** 与 ADJ-IN 对齐（条数差大才 ingest；否则 Watch 增量） |
| **FIB → BGP** | **只 diff**（相对 BGP 现网已通告集），**禁止** 对百万 prefix 无脑全量 AddPath |
| **restore-agent** | 恢复 **meta 中 enabled 邻居** 的会话；**不** 触发全量 `POST /api/rib/advertise` 扫库 |

---

## 10. 与现网差距（改造清单）

| 目标 | 现网 |
|------|------|
| upstream/downstream **FIB** + 选路 | ❌ 无 |
| **自动入库**（无 store 开关） | ❌ 需 `store_received_routes=1` |
| **自动 FIB diff 通告**（无 advertise 开关） | ❌ 需 `advertise_routes=1` + 手动任务 |
| BGP 管理 **仅启用/删除** | ❌ 有入库、通告按钮 |
| 断链不删 RIB；withdraw 门控 | ❌ withdraw 仍删 Peer RIB |
| RR 断链仍参与 upstream_fib | ❌ freeze/Sticky |
| 下游断链排除 downstream_fib | ❌ 未实现 |
| 删邻居 purge RIB + 重算 FIB | ⚠️ 部分 |
| SQLite 路由快照 / Sticky / peer_snapshot | 目标态 **删除** |
| `MTR_BGP_RESUME_ADVERTISE` 全量通告 | 目标态改为 **FIB diff reconcile** |

---

## 11. 实现分层

```text
L0  GoBGP ADJ-IN Watch / ingest
L1  Peer RIB（upstream / downstream）
L2  FIB Engine（SelectBest + 参与规则 + 持久化）
L3  Export（enabled 会话 TX/RX diff、kernel install）
L4  OP API（meta CRUD：增删改、enabled；查询代理；删除 purge 编排）
L5  Web BGP 管理（仅启用/删除；去掉入库、通告列）
```

---

## 12. 三条总规则

1. **RIB 按 peer 存、断链保留、管理删除才 purge peer。**  
2. **两张 FIB 分开持久化：upstream（RR 断链仍算、删除不算）/ downstream（断链不算、删除不算）。**  
3. **enabled 邻居自动入库 + 随 FIB diff 通告；所有下游会话共用 upstream_fib；配置只读库。**

---

## 13. 现网文档/API 目标态变更摘要

| 现网 | 目标态 |
|------|--------|
| `bgp_neighbor_meta.store_received_routes` | **删除**；enabled 即入库 |
| `bgp_neighbor_meta.advertise_routes` | **删除**；enabled 即随 FIB 通告 |
| `POST /api/bgp/neighbors/.../store-routes` | **删除** |
| `POST /api/bgp/neighbors/.../advertise` | **删除**（通告由 FIB engine 驱动） |
| `GET .../advertise/status` | **删除**或改为 FIB export 进度 |
| Web「路由入库」「路由通告」列/按钮 | **删除** |
| [OP_OPERATION_MANUAL.md](./OP_OPERATION_MANUAL.md) §4.4 | 待改为「仅启用/删除」 |

关联：[BGP_ARCHITECTURE.md](./BGP_ARCHITECTURE.md)（现网）、[BGP_DATA_AND_API.md](./BGP_DATA_AND_API.md)（待 §13 对齐）。
