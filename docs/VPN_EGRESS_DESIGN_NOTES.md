# VPN 出口管理 — 设计说明（v1）

> 对应实施清单 **第九章（文档交付）** 与开发计划 **M0**；实现以仓库代码为准，本文描述**当前行为**与**与 FRR 的边界**。

---

## 1. 范围与假设

- **场景**：本机（Linux 200 等）作为网关，**主动连接外部** OpenVPN / GRE / L2TP，将部分流量经隧道出口访问外网；**非**用户远程拨入。
- **默认 VRF**：隧道接口归属 **`vrf2103`**（库字段 `vpn_links.vrf` 可改，与 `step.md` 现网一致）。
- **控制面**：**SQLite**（`MTR_OP_DB`，默认 `service/data.db` 或部署路径 `/root/mtr_op/data.db`）。
- **正式前端**：[`service/static/index.html`](../service/static/index.html)；[`docs/admin-prototype.html`](./admin-prototype.html) 为原型，不接线。

---

## 2. SQLite 表结构

### 2.1 `vpn_links`

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | INTEGER PK | 自增 |
| `name` | TEXT UNIQUE | 隧道逻辑名（字母数字 `._-`） |
| `link_type` | TEXT | `openvpn` \| `gre` \| `l2tp` |
| `vrf` | TEXT | 默认 `vrf2103` |
| `endpoint` | TEXT | 展示/解析用：GRE 对端或 `host:port` |
| `iface_name` | TEXT | 内核接口名；空则插入后为 `mtrvpn{id}` |
| `enabled` / `desired_up` | INTEGER 0/1 | 启用与是否期望拉起 |
| `priority` | INTEGER | 数值越小越优先（仅排序展示/apply 顺序） |
| `config_json` | TEXT JSON | 类型专有参数（见 §4） |
| `last_error` / `last_rtt_ms` / `actual_status` | | 运行态 |
| `rx_bytes` / `tx_bytes` / `stats_updated_at` | | 计数缓存 |
| `created_at` / `updated_at` | TEXT ISO | |

### 2.2 `vpn_route_policies`

| 列 | 说明 |
|----|------|
| `dst_cidr` | 目的前缀（规范化 CIDR） |
| `src_cidr` | 源匹配，可为空；多段逗号分隔（校验时每段为合法 CIDR） |
| `src_label` | 展示用标签（如 C3），**不参与内核匹配** |
| `vpn_link_id` | FK → `vpn_links.id` |
| `backup_link_id` | 可选 FK；与 `fail_action=switch_backup` 配合 |
| `fail_action` | `fallback` \| `switch_backup` \| `deny` |
| `enabled` | 是否参与 `apply_all` 策略安装 |

### 2.3 `vpn_event_log`

追加式日志：`ts`, `kind`, `ref_id`, `message`（API/下发/策略等）。

---

## 3. REST API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/vpn/summary` | `total` / `up` / `down` / `disabled` |
| GET/POST | `/api/vpn/links` | 列表 / 创建 |
| GET/PATCH/DELETE | `/api/vpn/links/{id}` | 单条 |
| GET/POST | `/api/vpn/policies` | 列表 / 创建 |
| PATCH/DELETE | `/api/vpn/policies/{id}` | 更新；`backup_link_id` 清除见代码 `VPN_UNSET` |
| POST | `/api/vpn/apply` | 幂等：拆策略与隧道 → 按库重建 |
| POST | `/api/vpn/ping` | body: `target`, `vrf`, `count` — 在 VRF 内 ping |
| GET | `/api/vpn/events?limit=` | 最近事件 |

OpenAPI 细节以运行实例 **`/docs`** 为准。

---

## 4. `config_json` 约定（按 `link_type`）

### 4.1 `gre`

```json
{
  "gre": {
    "remote": "1.2.3.4",
    "local": "0.0.0.0",
    "ttl": 64,
    "mtu": 1476
  }
}
```

- `remote` 缺省时用 `endpoint` 的主机部分。

### 4.2 `openvpn`

```json
{
  "remote": "vpn.example.com:1194",
  "proto": "udp",
  "ca": "/abs/ca.crt",
  "cert": "/abs/client.crt",
  "key": "/abs/client.key"
}
```

- 生成 `data/vpn/openvpn-{id}.conf` 与 `mtr-openvpn-up.sh`，`--up` 将 `$dev` 并入 `vrf`。

### 4.3 `l2tp`

```json
{
  "l2tp": {
    "server": "1.2.3.4",
    "username": "user",
    "password": "secret",
    "password_file": "/abs/pass.txt",
    "ipsec_psk": "psk",
    "lac_name": "optional-lac-name"
  }
}
```

- 密码二选一：`password` 或可读 **`password_file`**（首行）。  
- 下发在 **`$MTR_OP_DATA/vpn/l2tp-{id}/`** 生成合并片段与 **`ip-up-vrf.sh`**；详见 [`vpn_egress.py`](../service/app/vpn_egress.py) 中 `apply_l2tp`。

---

## 5. 内核策略路由编号

实现模块：[`vpn_egress.py`](../service/app/vpn_egress.py)。

| 概念 | 默认公式 | 环境变量 |
|------|-----------|----------|
| 策略专用 **routing table** | `table_id = MTR_VPN_POLICY_TABLE_BASE + policy.id` | `MTR_VPN_POLICY_TABLE_BASE`（默认 **33700**） |
| **`ip rule` 优先级 pref** | `pref = MTR_VPN_POLICY_RULE_PREF_BASE + policy.id` | `MTR_VPN_POLICY_RULE_PREF_BASE`（默认 **28000**） |

- **与 FRR / 现网静态表冲突时**：在变更前执行 `ip route show table all`、`ip rule list` 做快照；将 `MTR_VPN_POLICY_TABLE_BASE` 挪到未占用段（避免与实验室 **2103** 等 per-VRF 表混淆，见 [`step.md`](../step.md)）。
- **规则内容**：有 `src_cidr` 时优先 `from <net> to <dst> lookup <table>`；解析失败则退化为 `to <dst> lookup <table>`。

### 5.1 `fail_action` 行为（策略安装时）

| 值 | 行为摘要 |
|----|-----------|
| `fallback` | 主隧道无接口时 **不写入**该策略 table/rule（依赖系统主路由，可能走普通出口） |
| `switch_backup` | 主隧道 iface 不存在则尝试 **backup_link_id** 对应 iface |
| `deny` | 无主备 iface 时在策略 table 内对 `dst` 安装 **`prohibit`** |

---

## 6. 与 FRR 的边界

| 项目 | OP / VPN 模块 | FRR（vtysh） |
|------|----------------|--------------|
| 邻居、AS、BGP RIB | 不修改 | **唯一权威**；OP 仅 [`/api/bgp/*`](../service/app/main.py) 读或写邻居元数据 |
| 客户路由转发 | 由内核 + FRR 下发路由决定 | VPN **不替换** BGP 配置 |
| 本功能写入项 | `ip tunnel` / `tun` / PPP、`ip rule`、`ip route table N`、OpenVPN 进程 | **不写** `frr.conf` |

VPN 策略仅影响 **匹配五元组/前缀的本地策略路由**；若需与 BGP 联动（例如隧道 down 撤回某前缀），属二期需求，不在当前实现范围。

---

## 7. 下发与幂等（`POST /api/vpn/apply`）

1. 对所有策略执行 **teardown**（`ip rule del pref …`，`ip route flush table …`）。  
2. 对所有 **GRE / OpenVPN / L2TP** 隧道执行类型 **teardown**（删接口/进程/配置目录等）。  
3. 按 `priority`、`id` 排序，对 `enabled && desired_up` 的隧道 **apply**。  
4. 对 `enabled` 的策略 **apply_policy**。  

非 Linux 或 **`MTR_OP_VPN_APPLY=0`**：跳过 `ip`/openvpn 调用，仅写库与日志。

---

## 8. 观测与后台任务

- **`MTR_VPN_RECONCILE`**（默认开启）：周期调用 `reconcile_status`，根据 **iface 是否存在** 刷新 `actual_status` 与 **rx/tx**（`/sys/class/net/.../statistics`）。  
- **L2TP**：配置不完整时 `last_error` 以 `l2tp_missing_*` / `l2tp_password_file_missing` 开头，`reconcile` 跳过以免抖动。

---

## 9. 关联文档

- [VPN_EGRESS_DEVELOPMENT_PLAN.md](./VPN_EGRESS_DEVELOPMENT_PLAN.md)  
- [VPN_EGRESS_OPS.md](./VPN_EGRESS_OPS.md)  
- [VPN_EGRESS_OPS.md](./VPN_EGRESS_OPS.md)  
- [VPN_EGRESS_IMPLEMENTATION_CHECKLIST.md](./VPN_EGRESS_IMPLEMENTATION_CHECKLIST.md)
