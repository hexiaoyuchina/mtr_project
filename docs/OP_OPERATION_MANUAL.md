# MTR-OP 操作手册（109 现网）

面向运维人员：说明 Web 各功能**做什么**、**怎么操作**、**做完后应看到什么**。  
管理地址：**http://101.89.68.109:8808/**（内网 SSH 到 109 后亦可 `curl` API）。

技术细节见：[MTR_TE_REWRITE.md](./MTR_TE_REWRITE.md)、[MTR_DOWNSTREAM_TRANSIT_109.md](./MTR_DOWNSTREAM_TRANSIT_109.md)、[部署.md](./部署.md)。

---

## 1. 系统组成（先建立概念）

| 组件 | 作用 | 你怎么感知 |
|------|------|------------|
| **OP（:8808）** | Web 配置、规则入库、下发 iptables/NFQUEUE、调 Agent | 浏览器管理页 |
| **bgp-agent（:9179）** | BGP 会话、百万级路由持久化（Redis+RocksDB） | BGP 管理 / BGP 路由页 |
| **te_rewrite_nfqueue** | 改写转发的 **ICMP Time Exceeded** 外层源 IP | MTR 中间跳显示「伪造 IP」 |
| **109 转发面** | 下联去程表 2110、回程表 2111、105.94 邻居 | 下游 mtr 能通、回程走 eno1np0 |

**两条业务线彼此独立：**

- **MTR 逐跳改写**：只改 **TE（type 11）外层源**，不改 Echo，不走 BGP 学路由。
- **BGP**：控制面学路由 / 入库 / 通告，与 MTR 显示无直接绑定。

---

## 2. 总览页

### 2.1 MTR/ICMP 逐跳替换总开关

| 项 | 说明 |
|----|------|
| **作用** | 打开后：安装 iptables NFQUEUE + 启动 `te_rewrite_nfqueue`，按「逐跳规则」改 TE 外层源；关闭后：TE 原样转发，**规则不生效**。 |
| **操作** | 总览 → 点击 **MTR/ICMP 逐跳替换** 开关。 |
| **预期效果** | 开：开关显示「已开启」；`pgrep te_rewrite_nfqueue` 有进程；mtr 中间跳可按规则显示伪造 IP。关：mtr 显示真实上游 hop，**不应**全 timeout（若关后仍 timeout，见 §7）。 |

### 2.2 ARP 引流总开关

| 项 | 说明 |
|----|------|
| **作用** | 在同二层发 GARP/代答，让客户端把「冒充网关 IP」解析到本机 MAC，流量经 109 转发。 |
| **操作** | 总览 → **ARP 引流** 开关。 |
| **预期效果** | 开：下游 `ip neigh` 见冒充 IP → 109 网口 MAC；配合 ARP 页条目可 ping 通冒充 IP。与 MTR 逐跳**独立**。 |

---

## 3. 逐跳替换规则

### 3.1 功能说明

- 客户端做 **mtr/traceroute** 时，路径经过 109 的 **ICMP Time Exceeded** 会被改写 **外层 IPv4 源地址**。
- **`match_cidr`**：必须对应 MTR 里显示的**那一跳真实 IP**（不是内层 Echo 地址）。
  - 例：MTR 第 N 跳显示 `142.251.67.15` → 规则写 `142.251.67.15/32`。
  - 写 `142.251.67.15/30` 表示从 `.15` 起连续 4 个地址（`.15～.18`）都映射到同一 `forged_src`。
- **`forged_src`**：希望 MTR 上显示的 IP。

### 3.2 新增规则

| 步骤 | 操作 |
|------|------|
| 1 | 确认总览 **逐跳总开关已开启** |
| 2 | 菜单 **逐跳替换规则** → 填写 **match_cidr**、**forged_src**、优先级、备注 → **添加** |

**预期效果：**

- 列表出现新行；几秒内生效（热加载映射，一般无需重启）。
- 下游对目标做 mtr，命中该 hop 的一跳显示为 **forged_src**。
- 109 上 `grep match_cidr /tmp/mtr_te_map.env` 可见 `真实IP=forged_src`。

### 3.3 修改规则

| 步骤 | 操作 |
|------|------|
| 1 | 列表点 **编辑** → 改 forged_src / match_cidr / 启用 → **保存** |

**预期效果：**

- 保存后 mtr **应显示新 forged_src**（若仍显示旧 IP，刷新页面再试；仍不对则 `tail /tmp/te_rewrite_nfqueue.log` 看是否有 `reload rules=…新值`）。
- 改 **forged_src** 不需关总开关。

### 3.4 停用 / 删除

| 操作 | 预期效果 |
|------|----------|
| **停用**（enabled=否） | 该条不再参与映射；mtr 该跳恢复真实 IP |
| **删除** | 映射中移除；mtr 该跳恢复真实 IP |

### 3.5 常见误判

| 现象 | 原因 | 处理 |
|------|------|------|
| mtr 全 `???` / timeout | NFQUEUE 无监听或守护未启动 | 开总开关；查 [MTR_TE_REWRITE.md](./MTR_TE_REWRITE.md) §排障 |
| 有跳但不改写 | 总开关关；match_cidr 与真实 TE 外层源不符 | 对照 mtr 该跳 IP 改规则 |
| 改 forged 仍显示旧 IP | 旧版热加载 bug（已修） | 部署最新 `te_rewrite_nfqueue` + OP |

---

## 4. BGP 管理

> **目标态**：仅 **启用 / 停用 / 删除邻居**；**无**「路由入库」「路由通告」开关（自动入库 + 随 FIB diff 通告）。见 [BGP_FIB_TARGET.md §5](./BGP_FIB_TARGET.md#5-bgp-管理界面目标态)。**下文为现网操作。**

### 4.1 功能说明

- 管理 **bgp-agent** 的 BGP 邻居：建会话、启停；现网另有 **路由入库**、**路由通告** 开关（目标态将去掉）。
- **角色**（下拉仅两项）：
  - **RR（路由反射客户端）**：连上游 RR，VRF 固定 **`gobgp-rr`**，TCP 源一般为 **207**（`139.159.43.207`）。
  - **下游运营商**：卫星/下联，VRF 为 **`vbgp*`**，TCP 源多为 ARP **冒充网关 IP**。

### 4.2 新增 RR 邻居

| 步骤 | 操作 |
|------|------|
| 1 | 角色选 **RR** → 邻居 IP、Remote AS → TCP 源填 **207** |
| 2 | **添加** |

**预期效果：**

- 列表出现邻居；会话 **Established**（需对端配置一致）。
- 开 **路由入库** 后，路由写入 Agent 持久库；**BGP 路由**页上游窗条数增加。

### 4.3 新增下游邻居

| 步骤 | 操作 |
|------|------|
| 1 | 先在 **ARP 引流** 配好冒充 IP + 卫星 VRF（若需 ipvlan） |
| 2 | BGP 管理 → 角色 **下游运营商** → 选 VRF、邻居 IP（如 208）、AS |
| 3 | TCP 源填冒充网关 IP → **添加** |

**预期效果：**

- 会话建立；下联 BGP 从冒充源发起。
- **路由入库** 开后，下游窗路由可查。

### 4.4 路由入库 / 路由通告（现网；目标态删除）

| 开关 | 作用 | 预期效果 |
|------|------|----------|
| **路由入库** | 把**对端通告给本机**的路由写入 Redis/RocksDB | BGP 路由页可查到该邻居前缀；KPI 条数上升 |
| **路由通告** | 从持久库读出，**向该邻居**再通告 | 对端 `show route` / 收到相应前缀 |
| **邻居启停** | 停 BGP 会话 | 会话 Down，不入库新路由 |

目标态下：**添加并启用邻居** 即自动入库；**FIB 变更** 自动向 enabled 会话 diff 通告，无需上表两开关。

### 4.5 编辑邻居

- 可改 Remote AS、角色、TCP 源、备注；改 IP 会删后重建，**短暂断会话**。

---

## 5. BGP 路由（只读查询）

### 5.1 功能说明

- **只读**查看 Agent 已持久化的学习路由（Redis + RocksDB），**不修改**路由表。
- **上游窗**：来自 RR 方向入库；**下游窗**：来自下游邻居入库。

### 5.2 分页浏览

| 步骤 | 操作 |
|------|------|
| 1 | 可选：**全部 / 上游窗 / 下游窗** |
| 2 | 可选：**VRF**、**来源邻居 IP**（二者为「或」关系，可只选其一） |
| 3 | 选每页条数 → **查询** → 翻页 |

**预期效果：**

- 表格列出前缀、下一跳、邻居、AS_PATH、更新时间等。
- 无数据时提示先开 **路由入库** 并等待同步。

### 5.3 前缀精确查询

| 步骤 | 操作 |
|------|------|
| 1 | 在 **前缀（精确）** 填 `8.8.8.8/32` 或 `8.8.8.0/24`（无掩码按 `/32`） |
| 2 | 可配合 VRF / 邻居缩小范围 → **查询** 或回车 |

**预期效果：**

- **库中有该前缀**：返回 **1～N 条**（不同 peer 各一条），前缀列与输入一致（规范化后）。
- **库中无**：**0 条**，提示未找到（**不会**再误显示无关前缀如 `1.0.0.0/24`）。
- 元数据显示 `前缀=…（精确）· 共 N 条`，分页禁用。

> 需 **新版 bgp-agent**（支持 `?prefix=`）；仅部署 OP 时精确查可能很慢或不准。

---

## 6. ARP 引流（摘要）

| 操作 | 预期效果 |
|------|----------|
| 添加条目：冒充网关 IP + 出接口 + 卫星 VRF | 周期 GARP；接口上挂 `/32`；可 ping 通冒充 IP |
| 开 ARP 总开关 | 守护进程发 GARP/代答 |
| 关 ARP 总开关 | 不再主动代答（已学到的 neigh 可能残留） |

卫星 VRF / ipvlan 细节见 [BGP_SATELLITE_IP_RULE_AND_DNAT.md](./BGP_SATELLITE_IP_RULE_AND_DNAT.md)。

---

## 7. 109 下游 MTR 转发（现场脚本）

与 Web 无关，保证 **105.94 客户端 mtr** 去程/回程正确。

| 操作 | 命令（开发机，已配 `109/env`） | 预期效果 |
|------|-------------------------------|----------|
| 应用 | `python 109/apply_downstream_transit.py` | pref 29/30、表 2110/2111、105.94 permanent neigh |
| 检查 | `python 109/apply_downstream_transit.py --check` | 输出 OK |
| 回退 | `python 109/apply_downstream_transit.py --teardown` | 去掉脚本写入的规则 |

详见 [MTR_DOWNSTREAM_TRANSIT_109.md](./MTR_DOWNSTREAM_TRANSIT_109.md)。

---

## 8. 代码部署（开发机）

在仓库根目录配置 `109/env`（含 `MTR_OP_SSH_PASSWORD`）。

| 场景 | 命令 | 预期效果 |
|------|------|----------|
| 只改 Web / Python / 逐跳规则 | `python tools/deploy_light.py --op-only` | 109 uvicorn 重启；**不**动 bgp-agent |
| 改 Go Agent | `python tools/deploy_light.py --agent-only` | 本地 WSL 编译 → 上传二进制 → `bgp-agent` 重启 |
| 全量（OP + Agent） | `python tools/deploy_light.py` | 两者均更新 |
| 仅同步 Agent unit | `python tools/sync_bgp_agent.py` | systemd 参数 + restart |

WSL 编译需：`$env:MTR_BGP_AGENT_WSL_DISTRO = "Ubuntu-24.04"`

---

## 9. 验收速查（SSH 到 109）

```bash
# OP
curl -s http://127.0.0.1:8808/health
curl -s http://127.0.0.1:8808/api/global          # hijack_enabled
curl -s http://127.0.0.1:8808/api/hop-rules | head

# TE 改写
pgrep -af te_rewrite_nfqueue
cat /proc/net/netfilter/nfnetlink_queue
iptables -t mangle -S FORWARD | grep NFQUEUE
grep . /tmp/mtr_te_map.env

# BGP Agent
curl -s http://127.0.0.1:9179/health
systemctl is-active bgp-agent

# 下游 mtr 路径
ip rule list | grep -E '29|30'
ip route get 8.8.8.8 from 139.159.105.94 iif eno1np0
```

---

## 10. 功能 → 操作 → 效果 一览表

| 功能 | 典型操作 | 成功时你看到 |
|------|----------|--------------|
| 逐跳总开关 | 总览打开 | mtr 可按规则改 hop；关则真实 hop |
| 逐跳规则 | 增删改 match/forged | mtr 对应跳变为 forged_src |
| BGP 邻居 | 加 RR/下游、启停 | 会话 Up/Down |
| 路由入库 | 开入库开关 | BGP 路由页有数据 |
| 路由通告 | 开通告 | 对端收到前缀 |
| BGP 路由查询 | VRF/邻居 + 查询 | 分页列表 |
| 前缀精确查 | 填前缀 + 查询 | 0 或若干条**完全匹配**前缀 |
| ARP 引流 | 加冒充 IP + 开关 | 下游 ARP 指向 109 |
| 下游 transit | apply 脚本 | 105.94 mtr 去回程正常 |

---

## 11. 相关文档

| 文档 | 内容 |
|------|------|
| [MTR_TE_REWRITE.md](./MTR_TE_REWRITE.md) | TE 改写原理与排障 |
| [MTR_DOWNSTREAM_TRANSIT_109.md](./MTR_DOWNSTREAM_TRANSIT_109.md) | 2110/2111、105.94 |
| [部署.md](./部署.md) | 发版与环境变量 |
| [BGP_OP_NETWORK.md](./BGP_OP_NETWORK.md) | 三网口与地址 |
| [NETDATA_MONITORING_109.md](./NETDATA_MONITORING_109.md) | 监控查看 |
