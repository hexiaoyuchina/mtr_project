# MTR/ICMP 运维 OP — 需求与实现说明

本文档用于记录当前实验环境的**业务需求**与**实现逻辑**，便于在**另一套网络环境**中按相同思路部署与验收。

---

## 1. 需求概述

### 1.0 Linux 200 功能定位（拦截范围与伪造边界）

**唯一机制（路径 A）**：上游路由器按 TTL 发出 **真实** ICMP Time Exceeded；包经 Linux 200 **转发**时，**`iptables` mangle + NFQUEUE** + **`te_rewrite_nfqueue.py`** 把 **外层 IPv4 源地址** 换成 OP 里配置的 **`forged_src`**。

- **不改 Echo**：不劫持、不合成 ICMP Echo；mtr 仍依赖真实上游 TE。
- **报表「替换」**：**`match_cidr` 须与 TE 外层源 IP 一致**（见 §4.5 连续地址展开）。
- **非目标**：不改变非 ICMP 业务转发语义；只影响 mtr 报表可见的逐跳地址（见 1.2）。

### 1.1 目标

- 在 Linux 200 **转发真实 ICMP TE** 时，按 **`hop_replace_rules`** 把 **TE 外层源 IP** 映射为 **`forged_src`**（未命中则直通）。
- 提供 **Web/API（FastAPI）**：**`hijack_enabled`**（TE 改写总开关）、逐跳规则（**`match_cidr` → `forged_src`、`priority`、`enabled`、备注**）。
- **ARP 引流（二层）**：对指定「网关 IPv4」发 GARP/定向 ARP Reply，使邻居将网关 IP 解析到 **200 出接口 MAC**（与路由表、TE 改写独立）。

### 1.2 非目标（边界）

- 不改变真实 IP 转发业务流量的语义描述 here；本方案聚焦 **ICMP/mtr 报表层面** 的可控展示与规则命中。
- 完整路径对齐依赖 **探测路径 + 可选前缀拼接**，需在环境中校准（见下文）。
- ARP 引流负责邻居表/MAC，与三层转发策略独立；UDP/TCP traceroute、命中审计等仍为非目标或未单独展开。

---

## 2. 架构概览

```
浏览器 / 运维脚本
        │ HTTP
        ▼
┌─────────────────────────────────────┐
│ Linux 200: FastAPI + SQLite          │
│  /api/global          hijack_enabled │
│  /api/hop-rules       CRUD → te_rewrite_sync │
│  /api/arp-spoof/…     ARP 引流        │
└──────────────┬──────────────────────┘
               │ sync_nft() + sync_te_rewrite_from_conn()
               ▼
┌─────────────────────────────────────┐
│ iptables mangle FORWARD/OUTPUT       │
│ icmp time-exceeded → NFQUEUE         │
│ te_rewrite_nfqueue.py（改 TE 外层源） │
└─────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ arp_spoof_daemon.py（独立）           │
└─────────────────────────────────────┘
```

关键文件（仓库内）：

| 组件 | 路径 |
|------|------|
| OP API / UI | `service/app/main.py`、`service/static/index.html` |
| nft 同步 + TE SNAT 占位 | `service/app/nft_sync.py`、`service/nft_mtr_te.nft` |
| **TE 映射生成与守护** | `service/app/te_rewrite_sync.py`、`service/app/hop_cidr.py` |
| **改写转发 ICMP TE** | `scripts/te_rewrite_nfqueue.py`（**建议系统 `python3` 运行**） |
| 持久化 | `service/app/storage.py`（SQLite） |
| ARP 引流守护 | `scripts/arp_spoof_daemon.py` |
| 轻量部署 | `tools/deploy_light_200.py` |

---

## 3. 功能总控与菜单边界

### 3.1 两个独立开关

**MTR 逐跳替换**（`hijack_enabled`）与 **ARP 引流**（`arp_spoof_enabled`）语义分离。

| 开关 | 作用 | 影响范围 |
|------|------|----------|
| `hijack_enabled` | TE 改写总开关：iptables NFQUEUE + **`te_rewrite_nfqueue`** + nft `ip mtr_te_snat` 占位 | 关闭时清空映射并停止 TE 守护 |
| `arp_spoof_enabled` | 二层宣告「网关 IP → 200 出接口 MAC」 | 仅 `arp_spoof_daemon.py` |

### 3.2 规则表边界

| 表/配置 | 用途 |
|---------|------|
| `global_config.hijack_enabled` | MTR 逐跳替换总开关 |
| `hop_replace_rules` | **`match_cidr`、`forged_src`、`priority`、`enabled`、备注**（API 对外）；库内仍有 legacy 列，新建/更新时写入默认 0/64 |
| `arp_spoof_settings`（`id=1`） | 仅 **`arp_spoof_enabled`**：ARP 引流总开关 |
| `arp_spoof_targets` | 多条：冒充网关 IPv4、出接口、策略、备注等（`spoof_gateway_ip` 全局唯一） |
| `gateway_reply_*`（旧表） | 历史遗留；当前逻辑不再读取 |

### 3.3 OP 总览与菜单建议

```text
总览（总开关：MTR 逐跳、ARP 引流）
MTR/ICMP 逐跳替换
ARP 引流：表格管理多条「冒充网关 IP + 出接口 + 被动策略」
```

## 4. MTR/ICMP 逐跳替换实现逻辑

### 4.1 转发 ICMP TE → `te_rewrite_nfqueue` 改写外层源

- **为何需要**：在 Linux 5.4 + VRF 等环境下，**转发的 ICMP Time Exceeded** 往往 **不进 nft NAT POSTROUTING**，nft SNAT 难以命中；真实改写依赖 **`iptables -t mangle`**（例：`FORWARD`、`icmp type time-exceeded`、`-o <出接口>`）→ **NFQUEUE**，用户态 **`scripts/te_rewrite_nfqueue.py`** 根据 **`MTR_TE_REWRITE_MAP`** 把 **外层 IPv4 源地址** `旧IP → forged_src`。
- **规则来源**：OP 启用中的 **`hop_replace_rules`**，由 **`build_rewrite_map_line`** 生成映射：按 **`hop_cidr.py`** 将 **`match_cidr`** 展开为 **`主机IP=伪造IP`**（「起始 IP + /前缀」连续地址语义，见 **§4.3**）。
- **持久化**：默认 **`/tmp/mtr_te_map.env`**（`export MTR_TE_REWRITE_MAP='…'`），可用 **`MTR_TE_REWRITE_MAP_FILE`** 覆盖。
- **何时刷新**：**POST/PATCH/DELETE `/api/hop-rules`** 成功后 **best-effort** **`sync_te_rewrite_from_conn`**（写映射后优先 **SIGHUP 热加载**；失败再冷启动并校验 bind；lifespan/总开关切换才全量整理 iptables）。**现网 109** 见 [MTR_DOWNSTREAM_TRANSIT_109.md](./docs/MTR_DOWNSTREAM_TRANSIT_109.md)、[MTR_TE_REWRITE.md](./docs/MTR_TE_REWRITE.md)。**`MTR_TE_REWRITE_SKIP_SYNC=1`**：本机开发可跳过。
- **实现**：`te_rewrite_nfqueue.py` **无 scapy**；勿再使用已删除的 **`mtr_spoof_nfqueue`**（路径 B）。
- **解释器**：重启 TE 守护进程时 **勿用 uvicorn 所在 venv**（常缺 **NetfilterQueue**）；实现默认 **`/usr/bin/python3`** 或 PATH 中 **`python3`**（**`MTR_TE_REWRITE_PYTHON`** 可覆盖），与 **`tools/deploy_light_200.py`** 使用 **`python3`** 拉起脚本一致。
- **无启用规则时**：映射为空；**`te_rewrite_nfqueue` 仍绑定队列并直通**，避免 NFQUEUE 无人监听导致转发 TE 被丢、mtr 中间跳 **`???`**。
- **nft `ip mtr_te_snat`**：`nft_sync.add_te_snat_rules` 可写 SNAT **占位**（实验室计数常为 0）；**报表以 iptables+NFQUEUE 改写为准**。

### 4.2 `hijack_enabled`

- **true**：安装 iptables mangle NFQUEUE、拉起 **`te_rewrite_nfqueue`**、写入 nft TE SNAT 占位。
- **false**：清空映射、拆除 NFQUEUE 规则、停止 TE 守护。

### 4.3 `hop_replace_rules` 与 `match_cidr`

- **只写 IPv4、无 `/`**：视为 **单主机**，等价 **`/32`**。
- **带 `/前缀`**：从 **所写的起始 IPv4** 起连续 **`2^(32-前缀)`** 个地址（与 **`ip_network(..., strict=False)` 对齐到网络号** 不同）。例：**`61.49.37.90/30`** 覆盖 **`.90～.93`**。
- **展开上限**：**`MTR_TE_REWRITE_MAX_EXPAND`**（默认 **4096**）；nft 侧 **`MTR_NFT_TE_SNAT_EXPAND_PER_RULE`** 等。
- **REST/UI**：`match_cidr`、`forged_src`、`priority`、`enabled`、`note`；库内 legacy 列（延时/TTL 等）写入默认值，**TE 改写不读**。

### 4.4 调试产物

- **`/tmp/mtr_te_map.env`**：TE 映射。
- **`/tmp/te_rewrite_nfqueue.log`**：守护进程日志。

---

## 5. ARP 引流（二层冒充网关 — 现行实现）

本节说明：**功能做什么**、**二层欺骗如何实现**、**为何还能 ping 通冒充 IP**、以及与 **MTR 逐跳劫持** 如何并存。

### 5.1 功能概述

| 能力 | 说明 |
|------|------|
| **总开关** | `arp_spoof_settings.arp_spoof_enabled`，在 Web **总览** 切换；关闭后守护进程不再发 GARP/被动应答（读库轮询）。 |
| **多条配置** | 表 **`arp_spoof_targets`**：每条包含 **冒充网关 IPv4**（`spoof_gateway_ip`，全局唯一）、**出接口**（`egress_iface`）、是否启用、**被动应答策略**（`gateway_only` / `victim_cidr`）、策略网段、备注等。 |
| **运维界面** | **ARP 引流** 页：表格展示条目；**添加/编辑** 对话框填写网关 IP、**接口（下拉，来源 `/api/host-ifaces`）**、策略与备注；启用表示参与 **周期 GARP + 被动 ARP 应答**（与守护进程逻辑一致）。 |

**边界**：ARP 引流只改变 **同一二层域内**「某个 IPv4 → MAC」的解析结果；**不修改** 客户端或其它主机上的 **路由表**。要让流量 **经 Linux 200 转发**，仍需客户端把 **默认网关或静态路由下一跳** 指到所冒充的 IP（或等价策略路由）。

---

### 5.2 二层「欺骗」如何实现（`scripts/arp_spoof_daemon.py`）

守护进程读 **`MTR_OP_DB`**（与 OP 同一 SQLite），仅在 **`arp_spoof_enabled = true`** 且存在 **启用条目** 时工作。

1. **周期性 GARP（主动）**  
   对每个启用条目的 `(egress_iface, spoof_gateway_ip)`：在 **该出接口** 上用 **接口真实 MAC**（`/sys/class/net/<iface>/address`）发送 **Gratuitous ARP**，宣告「`spoof_gateway_ip` 对应本 MAC」。间隔由 **`--garp-interval`**（默认 10s，可用环境变量 **`MTR_ARP_GARP_INTERVAL`**）控制。

2. **被动 ARP Reply（应答询问）**  
   按 **出接口** 分别嗅探 ARP **who-has**。若询问的 **`pdst`** 等于某条配置的 **`spoof_gateway_ip`**，且策略允许：
   - **`gateway_only`**：不筛选询问方 IP，一律回复；
   - **`victim_cidr`**：仅当询问源 **`psrc`** 落在 **`policy_cidrs`** 所列 CIDR 内才回复（**留空**表示不筛源，与仅网关语义接近时需自行权衡）。

3. **配置热更新**  
   默认每隔 **`--reload-sec`**（如 5s）重读库；OP 写入配置时可更新时间戳文件 **`MTR_ARP_RELOAD_FILE`**（见环境变量表）以便更快生效。

**结果**：同网段主机执行 **`ip neigh`** / **`arp -n`** 时，配置的 **冒充网关 IP** 应显示为 **Linux 200 对应出接口的 MAC**（在无其它设备抢答的前提下）。

---

### 5.3 为何能 ping 通「冒充网关 IP」（三层）

仅 ARP 只能保证 **以太网帧发到 200 的 MAC**；若内核没有把该 IPv4 当作 **本机地址**，通常 **不会** 正常回复 **ICMP Echo**，表现为 ping 不通。

当前方案采用 **两处配合**：

**（A）接口上挂主机地址（默认开启）**  

守护进程在启用条目下周期性执行等价逻辑：若接口上尚无该地址，则  

`ip addr add <spoof_gateway_ip>/32 dev <egress_iface>`  

使 Linux 200 **协议栈认定该 IP 为本机**，从而能回复 Echo。  

- 关闭方式：环境变量 **`MTR_ARP_ASSIGN_HOST_IP=0`**（或 **`false`/`no`**），或启动参数 **`--no-assign-host-ip`**。  
- **注意**：仅自动 **添加**；删除 OP 条目 **不会** 自动 `ip addr del`，避免误删手工配置；若出现 **地址冲突**（网上另有真机占用同 IP），需调整配置或关闭自动加地址。

**（B）与 MTR TE 改写的关系**  

现网 **仅 ICMP Time Exceeded（type 11）** 进 NFQUEUE 做逐跳改写，**Echo-request 不进 TE 队列**。ping **冒充网关 IP** 依赖 **（A）** 在接口上挂 `/32` 后由内核正常应答；与 TE 改写正交。详见 [MTR_TE_REWRITE.md](./docs/MTR_TE_REWRITE.md)。

---

### 5.4 数据模型摘要

**`arp_spoof_settings`（单行）**

| 字段 | 说明 |
|------|------|
| `arp_spoof_enabled` | ARP 引流总开关 |

**`arp_spoof_targets`（多行）**

| 字段 | 说明 |
|------|------|
| `spoof_gateway_ip` | 要冒充的网关 IPv4（全局唯一） |
| `egress_iface` | 发二层帧的接口；MAC 取自该口 |
| `enabled` | 是否参与 GARP + 被动应答 +（默认）自动 `/32` |
| `policy_mode` / `policy_cidrs` | 被动应答筛选（见 §5.2） |

旧版单行 JSON 字段若存在，迁移逻辑会将历史 **`spoof_gateway_ips`** 拆成多行目标（见存储层 **`_migrate_arp_spoof_targets`**）。

---

### 5.5 验收建议（客户端如 Linux 201）

1. **`ip neigh show <网关IP>`**（或 **`arp -n`**）：**`lladdr`** 应为 **Linux 200 所选出接口的 MAC**。  
2. **`ping -I <客户端业务口> <冒充网关IP>`**：在 **（A）（B）** 生效时应 **可通**；若不通，检查 200 上是否 **`ip addr`** 已有 **`/32`**、nft 是否包含该 IP 的 bypass、防火墙及 IP 冲突。  
3. OP **总开关**、条目 **启用** 状态与现场路由是否指向该网关 IP 一致。

---

### 5.6 与旧「NFQUEUE 网关 ICMP Echo Reply」的关系

原在 NFQUEUE 内对指定目的直接 **伪造 Echo Reply** 的分支已移除；**Ping 通冒充 IP** 依赖 **本机 `/32` + nft 放行**，而非 NFQUEUE 代答 ICMP。

---

## 6. 客户端视角：三段路径的逻辑（模拟）

以下用 **虚构 IP** 说明「谁在回 TE、报表上显示谁」，不涉及真实拓扑备案。

假设：

- 客户端 `C` 向目的 `D`（如公网）发 Echo，TTL 递增。
- 某一跳路由器发出 **真实 ICMP TE**，外层源为 **`R`**；若 **`R`** 命中 OP **`match_cidr` 展开后的主机地址**，经 Linux 200 转发时 **`te_rewrite_nfqueue`** 把外层源改为 **`forged_src`**，mtr 显示伪造地址。
- **未命中映射**：TE 保持原外层源，mtr 显示真实 hop IP。

---

## 7. 迁移到另一套环境时的检查清单

1. 确认 **转发的 ICMP TE** 经过 Linux 200；**iptables mangle** 将 **time-exceeded** 送入 **NFQUEUE**；**`te_rewrite_nfqueue.py`** 使用 **系统 `python3`** 且已安装 **NetfilterQueue、scapy**。
2. **`hijack_enabled=true`** 且 **`pgrep te_rewrite`** 存在；**勿**再运行 **`mtr_spoof_nfqueue`**（会与 queue 1 冲突）。
3. **规则**：**`match_cidr`** 覆盖 **真实 TE 外层源**；注意 **§4.3** 连续地址语义。
4. **防火墙**：放行 OP **HTTP**（如 8808）。
5. **ARP 引流**：`arp_spoof_daemon.py`、`arp_spoof_targets`、§5.3 验收。

---

## 8. 部署与运维（操作说明另见）

轻量同步到现网 VR：**`tools/deploy_light.py`**（**`--op-only`** 仅 OP）；环境变量 **`MTR_OP_HOST`**、**`MTR_OP_SSH_PASSWORD`**。步骤见 [`docs/部署.md`](./docs/部署.md)、TE 排障见 [`docs/MTR_TE_REWRITE.md`](./docs/MTR_TE_REWRITE.md)。

---

## 9. 参考命令与环境变量（摘录）

| 项 | 说明 |
|----|------|
| `MTR_OP_DB` | SQLite 路径 |
| `MTR_OP_NFT` | nft 规则文件路径 |
| `MTR_TE_REWRITE_MAP_FILE` | TE 映射 env 路径（默认 `/tmp/mtr_te_map.env`） |
| `MTR_TE_REWRITE_PYTHON` | 拉起 **`te_rewrite_nfqueue`** 的解释器（默认选系统 **`python3`**） |
| `MTR_TE_REWRITE_SKIP_SYNC` | `1`：API 不执行 **`te_rewrite_sync`**（本机开发） |
| `MTR_TE_REWRITE_MAX_EXPAND` | 单条 `match_cidr` 最多展开主机数（默认 4096） |
| `MTR_NFT_BYPASS_MAX` | nft 合并绕过规则数量上限（默认 256） |
| **ARP 总开关** | DB：`arp_spoof_enabled`（§5.4）；REST：`PUT /api/arp-spoof/settings` |
| **`MTR_ARP_ASSIGN_HOST_IP`** | `1`（默认）：守护进程为每条启用目标尝试 **`ip addr add …/32`**；`0`/`false`/`no`：不加主机地址 |
| `MTR_ARP_GARP_INTERVAL` | 周期性 GARP 间隔（秒） |
| `MTR_ARP_RELOAD_SEC` | 守护读库周期（秒） |
| `MTR_ARP_RELOAD_FILE` | OP 更新 ARP 配置时可写时间戳，便于守护更快 reload |

部署见 [`docs/部署.md`](./docs/部署.md)；TE 排障见 [`docs/MTR_TE_REWRITE.md`](./docs/MTR_TE_REWRITE.md)；背景见 **`service/README.md`**；实验见 **`step.md`**。

---

*文档版本：仅 **路径 A**（`te_rewrite_nfqueue` 改写真实 TE 外层源）；已移除路径 B（`mtr_spoof_nfqueue` Echo 合成 TE）。*
