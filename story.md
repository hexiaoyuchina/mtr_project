# MTR/ICMP 运维 OP — 需求与实现说明

本文档用于记录当前实验环境的**业务需求**与**实现逻辑**，便于在**另一套网络环境**中按相同思路部署与验收。

---

## 1. 需求概述

### 1.0 Linux 200 功能定位（拦截范围与伪造边界）

实验环境里存在 **两条可选链路**，都会在 **mtr 报表**上体现「某一跳的 IPv4」，但机制不同：

| 路径 | 含义 | 典型进程 |
|------|------|-----------|
| **A. 改写真实 ICMP TE**（**现行主推**） | 上游路由器按 TTL 发出 **真实** ICMP Time Exceeded；包经 Linux 200 **转发**时，用 **`iptables` mangle + NFQUEUE** 在用户态 **`te_rewrite_nfqueue.py`** 把 **外层 IPv4 源地址** 换成 `forged_src` | `te_rewrite_nfqueue.py`，规则来自 OP `hop_replace_rules` |
| **B. 用户态合成 ICMP TE**（可选） | 将 **ICMP Echo-request** 送入 NFQUEUE，由 **`mtr_spoof_nfqueue.py`** 按探测链 **合成** ICMP TE，外层源用 forged 或真实探测 IP | `mtr_spoof_nfqueue.py`，总开关 `hijack_enabled` |

- **路径 B 拦截什么**：针对被 nftables 送入 NFQUEUE 的 **ICMP Echo Request**（mtr/ping）。**`hijack_enabled=false`** 时 Echo 不进该队列，行为与未部署用户态合成时一致。
- **路径 A 不改 Echo**：只处理 **转发的 ICMP Time Exceeded**；与 Echo 是否进队列无关。
- **报表上「替换」**：路径 A 依赖 **真实 TE 的外层源 IP** 命中映射（OP 里 **`match_cidr` 与 TE 源一致**）；路径 B 依赖 **HopStore 链与 TTL 索引** 对齐。
- **非目标**：不改变非 ICMP 业务转发的语义；本方案只影响 **ICMP/mtr 报表层面**可见的逐跳地址（见 1.2）。

### 1.1 目标

- **路径 A**：在 Linux 200 **转发真实 ICMP TE** 时，按 **`hop_replace_rules`** 把 **TE 外层源 IP** 映射为 **`forged_src`**（或直通未命中映射的地址）。
- **路径 B（可选）**：在 Linux 200 **劫持 ICMP Echo** 时，由用户态按规则 **合成 ICMP TE**，使报表显示 forged 或探测得到的 hop IP。
- 提供 **Web/API（FastAPI）** 管理：MTR 逐跳替换总开关、逐跳规则（**`match_cidr` → `forged_src`、`priority`、`enabled`、备注**）；**API/UI 已移除** 仅与路径 B 合成 TE 相关的延时、TTL、丢包、抖动等字段（库表仍保留对应列，写入默认值）。
- **ARP 引流（二层）**：在 Linux 200 上按配置对指定「网关 IPv4」发送 Gratuitous ARP / 定向 ARP Reply，使同一二层域内主机将「网关 IP → MAC」解析到 **200 出接口的真实 MAC**（不替代路由表，只解决邻居解析）。
- **路径 B** 仍由 **nftables NFQUEUE** + **`mtr_spoof_nfqueue.py`** 处理 ICMP Echo→合成 TE；已移除基于 NFQUEUE 的 **L3 ICMP 网关 Echo Reply 代答**。

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
               │ sync_nft()；hop 规则变更时 sync_te_rewrite_from_conn()
               ▼
┌─────────────────────────────────────┐  ┌──────────────────────────────────┐
│ iptables mangle FORWARD             │  │ nftables: inet mtr_spoof          │
│ icmp time-exceeded → NFQUEUE         │  │ hijack 开：Echo-request → queue   │
│ te_rewrite_nfqueue.py（改 TE 外层源） │  │ → mtr_spoof_nfqueue（可选）       │
└─────────────────────────────────────┘  └──────────────────────────────────┘
               │                                      │
               ▼                                      ▼
┌─────────────────────────────────────┐  （路径 B 未部署时可为空）
│ arp_spoof_daemon.py（独立）           │
└─────────────────────────────────────┘
```

关键文件（仓库内）：

| 组件 | 路径 |
|------|------|
| OP API / UI | `service/app/main.py`、`service/static/index.html` |
| nft 同步 + TE SNAT 占位 | `service/app/nft_sync.py`、`service/nft_mtr_spoof.nft` |
| **TE 映射生成与重启守护** | `service/app/te_rewrite_sync.py`、`service/app/hop_cidr.py` |
| **改写转发 ICMP TE** | `scripts/te_rewrite_nfqueue.py`（**建议系统 `python3` 运行**） |
| 持久化 | `service/app/storage.py`（SQLite） |
| Echo→合成 TE（可选） | `scripts/mtr_spoof_nfqueue.py` |
| ARP 引流守护 | `scripts/arp_spoof_daemon.py` |
| 轻量部署 | `tools/deploy_light_200.py` |

---

## 3. 功能总控与菜单边界

### 3.1 两个独立开关

**MTR 逐跳替换**（`global_config.hijack_enabled`）与 **ARP 引流**（`arp_spoof_settings.arp_spoof_enabled`）语义分离：**nft 是否把 ICMP Echo 送进 NFQUEUE 仅由逐跳开关决定**；ARP 不再参与 NFQUEUE。

| 开关 | 作用 | 影响范围 |
|------|------|----------|
| `hijack_enabled` | **路径 B**：Echo→NFQUEUE→**`mtr_spoof_nfqueue` 合成 TE** | 仅影响 Echo 是否进队列；**路径 A（真实 TE 改写）不依赖此项** |
| `arp_spoof_enabled` | 二层宣告「网关 IP → 200 出接口 MAC」 | 仅 `arp_spoof_daemon.py`；与 nft 队列无关 |

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

### 3.4 nft prerouting 与 NFQUEUE（ICMP Echo）

**先于用户态**：在 **nft** 链中，对「目的为本机接口 IPv4」以及 **数据库里启用中的 ARP `spoof_gateway_ip`** 的 **ICMP Echo-request** 直接 **accept**（不进队列），避免与 vrf 下的「ping 本机」冲突，并保证对「冒充网关 IP」ping 时仍走内核正常 Echo Reply。

**再进 NFQUEUE**（仅当 **hijack_enabled = true** 且未被上述规则放行）：`mtr_spoof_nfqueue.py` 按 HopStore + 规则合成 TE / AcceptFinal / drop。

ARP 二层宣告不在此路径内；由 **`arp_spoof_daemon.py`** 独立进程完成。

---

## 4. MTR/ICMP 逐跳替换实现逻辑

### 4.1 路径一（主推）：转发 ICMP TE → `te_rewrite_nfqueue` 改写外层源

- **为何需要**：在 Linux 5.4 + VRF 等环境下，**转发的 ICMP Time Exceeded** 往往 **不进 nft NAT POSTROUTING**，nft SNAT 难以命中；真实改写依赖 **`iptables -t mangle`**（例：`FORWARD`、`icmp type time-exceeded`、`-o <出接口>`）→ **NFQUEUE**，用户态 **`scripts/te_rewrite_nfqueue.py`** 根据 **`MTR_TE_REWRITE_MAP`** 把 **外层 IPv4 源地址** `旧IP → forged_src`。
- **规则来源**：OP 启用中的 **`hop_replace_rules`**，由 **`service/app/te_rewrite_sync.py`** 的 **`build_rewrite_map_line`** 生成映射：仅 **IPv4**，按 **`service/app/hop_cidr.py`** 将每条 **`match_cidr`** 展开为多个 **`主机IP=伪造IP`**（与 **`mtr_spoof_nfqueue._match_cidr_span` 同一套「起始 IP + /前缀」连续地址语义**，不是 `ip_network` 强行对齐——见 **§4.5**）。
- **持久化**：默认 **`/tmp/mtr_te_map.env`**（`export MTR_TE_REWRITE_MAP='…'`），可用 **`MTR_TE_REWRITE_MAP_FILE`** 覆盖。
- **何时刷新**：**POST/PATCH/DELETE `/api/hop-rules`** 成功后 **best-effort** 调用 **`sync_te_rewrite_from_conn`**（失败只打日志）；服务 **lifespan 启动**亦尝试一次。**`MTR_TE_REWRITE_SKIP_SYNC=1`**：本机开发可跳过。
- **解释器**：重启 TE 守护进程时 **勿用 uvicorn 所在 venv**（常缺 **NetfilterQueue**）；实现默认 **`/usr/bin/python3`** 或 PATH 中 **`python3`**（**`MTR_TE_REWRITE_PYTHON`** 可覆盖），与 **`tools/deploy_light_200.py`** 使用 **`python3`** 拉起脚本一致。
- **无启用规则时**：映射为空；**`te_rewrite_nfqueue` 仍绑定队列并直通**，避免 NFQUEUE 无人监听导致转发 TE 被丢、mtr 中间跳 **`???`**。
- **nft `ip mtr_te_snat`**：`nft_sync.add_te_snat_rules` 可写 SNAT **占位**（实验室计数常为 0）；**报表以 iptables+NFQUEUE 改写为准**。

### 4.2 路径二（可选）：ICMP Echo → NFQUEUE → `mtr_spoof_nfqueue` 合成 TE

依赖 **`hijack_enabled=true`**、**`mtr_spoof_nfqueue.py` 在跑**、nft 将 Echo 送入队列（**`nft_mtr_spoof.nft`**）。总开关即 OP **`/api/global`**。

### 4.3 `hijack_enabled`（仅路径二）

- **true**：加载 **ICMP Echo-request → NFQUEUE**（见 `nft_mtr_spoof.nft`）。
- **false**：Echo **不进该队列**。
- **顺序**：先起 **监听队列的进程**，再加载/更新 nft，避免队列无人接管。

### 4.4 `hop_replace_rules`（对外字段）

- **REST/UI**：`match_cidr`、`forged_src`、`priority`、`enabled`、`note`，以及列表里的 `id`、`created_at`。
- **SQLite**：仍保留 `delay_*`、`icmp_ip_ttl`、`loss_*`、`jitter_*` 等列；**新建/更新时存储层写入默认值**，仅供 **`mtr_spoof_nfqueue`** 若读库时使用；**路径一 TE 改写不读延时/TTL**。
- **路径二匹配**：对每个探测 hop IP，**priority 高者优先**；同 priority **前缀更长者优先**。

### 4.5 `match_cidr` 写法（路径一、二共用）

- **只写 IPv4、无 `/`**：视为 **单主机**，等价 **`/32`**。
- **带 `/前缀`**：从 **所写的起始 IPv4** 起连续 **`2^(32-前缀)`** 个地址（与 **`ip_network(..., strict=False)` 对齐到网络号** 不同）。例：**`61.49.37.90/30`** 覆盖 **`.90～.93`**。
- **展开上限**：**`MTR_TE_REWRITE_MAX_EXPAND`**（默认 **4096**）；nft 侧另有 **`MTR_NFT_TE_SNAT_EXPAND_PER_RULE`** 等。

### 4.6 探测与 Hop 链（仅路径二 `mtr_spoof_nfqueue`）

- **probe_loop** 周期性（默认 `--probe-interval` ≥45s）对 NFQUEUE 见过的 Echo **目的 IP** 探测路径（本机或 SSH 验证机 **`ip vrf exec … mtr`**）。
- **同步探测**（`--sync-probe` / **`MTR_PROBE_SYNC=1`**）：每个 Echo 在回调里现跑 mtr/traceroute；**`MTR_PROBE_SYNC_CACHE_SEC`** 控制复用秒数。
- **`???` 跳**：占位不参与规则匹配，合成 TE 外层源可用 **RFC 5737 `192.0.2.x`**。
- **路径末尾补齐目的 IPv4**；可选 **`/tmp/mtr_spoof_chain.json`**（`--cache-file`）。
- **`build_hops_from_probe`**：中间跳匹配规则生成 **`HopEntry`**。
- **`MTR_HOP_PREFIX_IPS`**：探测 path 前拼接前缀 IP，与客户端 **`hop_index = ttl - 1`** 对齐。

### 4.7 NFQUEUE 回调（仅路径二）

- 处理 **ICMP Echo-request**，**`hop_index = ttl - 1`**，查 **`HopStore`**；**`--max-synthetic-hops`**（默认 32）截断；合法索引内 **合成 TE** 并 **drop** 原 Echo，否则 **accept**。**`--cache-miss-action`** 控制未缓存行为。

### 4.8 规则/Hop 链刷新（路径二）

- **RuleCache**：约每 **`--rules-reload-sec`**（默认 5s）读库。
- **Hop 链**：**probe_loop** 周期或同步探测模式更新。

### 4.9 调试产物

- **`/tmp/mtr_te_map.env`**：路径一映射。
- **`/tmp/mtr_spoof_chain.json`**：路径二 hops/probed 对照。

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

**（B）nft 放行发往「冒充 IP」的 ICMP Echo（与 MTR 劫持共存）**  

当 **MTR 逐跳替换** 打开时，IPv4 Echo-request 默认会进 **NFQUEUE**。为避免 **ping 本机接口 / ping 冒充网关 IP** 被用户态误处理，**`service/app/nft_sync.py`** 在队列规则之前下发：

- 对所有 **本机接口 IPv4**（`ip -j addr` 枚举，含 vrf 接口）；  
- 对所有 **`arp_spoof_targets` 中启用条目的 `spoof_gateway_ip`**；  

逐条 **`ip daddr <地址> icmp type echo-request accept`**。  

因此：**发往这些 IP 的 ping 不进入 NFQUEUE**，由内核按正常路径回复。

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
- **路径 A**：某一跳路由器发出 **真实 ICMP TE**，外层源为 **`R`**；若 **`R`** 命中 OP **`match_cidr` 展开后的某一主机地址**，经 Linux 200 转发时 **`te_rewrite_nfqueue`** 可把外层源改为 **`forged_src`**，mtr 显示伪造地址。
- **路径 B**：分组到达 **Linux 200** 后 **ICMP Echo 进入 NFQUEUE**，OP 已为 `D` 生成 hop 链（含前缀 + 探测），例如：

| hop_index | 含义（逻辑位置） | 链中 icmp_src（示例） | 备注 |
|-----------|------------------|------------------------|------|
| 0 | 第一跳 | `10.133.152.200` | 可与前缀或真实探测一致 |
| 1 | 第二跳 | `10.133.153.204` | |
| 2 | 进入公网后某一跳 | `200.0.0.2` | **规则 forged** |
| … | … | … | |

**Linux 200 之前**：TTL 较小时，Echo 在 **上游真实设备** 上耗尽 → 客户端收到 **真实路由器** 发出的 ICMP TE → mtr 显示 **真实 hop IP**。

**Linux 200 上（路径 B，劫持生效且 TTL 命中由 200 代答时）**：由 **`mtr_spoof_nfqueue` 合成 TE**，mtr 显示链表中 **`icmp_src`**（可能为 forged）。

**Linux 200 到目的 IP**：若后续 TTL 仍由 **同一 HopStore 链** 覆盖，则仍显示链表中各项；若 **hop_index ≥ len(hops)**，代答逻辑 **放行原包**，之后可能收到真实 hop 或最终 Echo Reply。

**hop_index 更大**：表示 **TTL 更大**（离客户端更远的一跳）。链必须足够长且与真实 TTL 消耗一致；否则会出现索引错位或落入 accept/fallback 分支。

---

## 7. 迁移到另一套环境时的检查清单

1. **路径 A**：确认 **转发的 ICMP TE** 会经过 Linux 200；**iptables mangle FORWARD** 将 **time-exceeded** 送入 **NFQUEUE**；**`te_rewrite_nfqueue.py`** 使用 **系统 `python3`** 且已 **`pip install NetfilterQueue scapy`**。
2. **路径 B（若启用）**：确认 **ICMP Echo** 到达 200 且 nft **prerouting** 能命中 Echo；**`mtr_spoof_nfqueue`** 与 **`hijack_enabled`** 一致。
3. **依赖**：Python、`nftables`、NetfilterQueue、scapy；**`MTR_OP_DB`** 与守护进程 **`--op-db`** 一致。
4. **探测（路径 B）**：是否 **SSH 到验证机**（`MTR_PROBE_SSH_HOST`、`MTR_PROBE_VRF_EXEC`）；**`MTR_HOP_PREFIX_IPS`** 与客户端 TTL 对齐。
5. **规则**：**`match_cidr`** 覆盖 **真实 TE 外层源**（路径 A）或 **探测 hop IP**（路径 B）；注意 **§4.5** 连续地址语义。
6. **防火墙**：放行 OP **HTTP**（如 8808）。
7. **启动顺序**：先起 **NFQUEUE 监听进程**，再加载 nft（路径 B）；路径 A 同理避免队列空闲异常。
8. **ARP 引流**：`arp_spoof_daemon.py`、`arp_spoof_targets`、§5.3 验收。

---

## 8. 部署与运维（操作说明另见）

轻量同步代码到 Linux 200、写 **`/tmp/mtr_te_map.env`**、拉起 **`te_rewrite_nfqueue`/`uvicorn`/ARP 守护**：**`tools/deploy_light_200.py`**（环境变量 **`MTR_OP_HOST`**、**`MTR_OP_SSH_PASSWORD`**）。更多步骤见仓库 **`deploy.md`**（若存在）。本文档不重复命令。

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
| `MTR_PROBE_SSH_HOST` | 路径 B 探测用 SSH 主机（可选） |
| `MTR_PROBE_VRF_EXEC` | SSH 侧 vrf 前缀 |
| `MTR_HOP_PREFIX_IPS` | 路径 B 探测路径前缀 IP（逗号分隔） |
| `--rules-reload-sec` | **`mtr_spoof` 规则热加载间隔**（默认 5） |
| `--probe-interval` | **`mtr_spoof` 探测周期**（默认 45） |
| **ARP 总开关** | DB：`arp_spoof_enabled`（§5.4）；REST：`PUT /api/arp-spoof/settings` |
| **`MTR_ARP_ASSIGN_HOST_IP`** | `1`（默认）：守护进程为每条启用目标尝试 **`ip addr add …/32`**；`0`/`false`/`no`：不加主机地址 |
| `MTR_ARP_GARP_INTERVAL` | 周期性 GARP 间隔（秒） |
| `MTR_ARP_RELOAD_SEC` | 守护读库周期（秒） |
| `MTR_ARP_RELOAD_FILE` | OP 更新 ARP 配置时可写时间戳，便于守护更快 reload |

部署操作见 **`deploy.md`**；背景与故障片段见 **`service/README.md`**、拓扑与实验步骤见 **`step.md`**。

---

*文档版本：已写入 **路径 A（`te_rewrite_nfqueue` + `hop_cidr` + OP 同步）** 与 **路径 B（`mtr_spoof_nfqueue` + `hijack_enabled`）**；OP 对外 hop 规则已精简字段；§5 ARP 与 nft Echo 放行、`arp_spoof_daemon` 自动 `/32` 等与现行代码一致；原 NFQUEUE 网关 ICMP Echo Reply 代答已移除。*
