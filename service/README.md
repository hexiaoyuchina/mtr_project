# service — MTR/ICMP 运维 OP

本目录包含 **FastAPI 后端**（[`app/`](app/)）、**SQLite**、**nft set 同步**、以及正式业务界面 **[`static/index.html`](static/index.html)**（由 `GET /` 同源提供，调用 `/api/*`）。[`docs/admin-prototype.html`](../docs/admin-prototype.html) **仅为产品原型/需求示意**，不随服务发布；视觉可参考，功能以 `static/index.html` 为准。

---

## 访问地址（现网 VR）

| 用途 | URL |
|------|-----|
| 管理首页 | `http://101.89.68.109:8808/` |
| OpenAPI | `http://101.89.68.109:8808/docs` |
| 健康检查 | `GET http://101.89.68.109:8808/health` |

（端口可通过 systemd 或启动命令改为其它值；须放行防火墙 TCP **8808**。）

---

## 一键部署与验证

在**能 SSH 到实验室管理网**的机器上（仓库根目录）：

```bash
pip install paramiko
set MTR_OP_HOST=101.89.68.109
set MTR_OP_SSH_PASSWORD=<密码>
python tools/deploy_light.py
```

新机全量：`python service/scripts/deploy_bgp_rxtx.py`（见 [`docs/部署.md`](../docs/部署.md)）。

环境变量：`MTR_OP_SSH_PASSWORD`、`MTR_OP_HOST`（默认 `101.89.68.109`）。

**常见问题**：**`te_rewrite_nfqueue.py` 不依赖 scapy**（系统 `python3` + NetfilterQueue 即可）。**ARP 守护**若需 scapy，部署脚本优先 **apt `python3-scapy`**，避免 **`pip install scapy`** 与 **`cryptography`** 冲突。API 若 **`python3 -m venv` 失败**，脚本会改用系统 **`pip3 install fastapi uvicorn`**。

仅改 OP/TE、不动 bgp-agent：`python tools/deploy_light.py --op-only`（见 [`docs/部署.md`](../docs/部署.md)）。

systemd 示例：[`systemd/mtr-op.service`](systemd/mtr-op.service)（`WorkingDirectory=/root/mtr_op`）。

---

## 业务界面

| 项目 | 说明 |
|------|------|
| **路径** | [`static/index.html`](static/index.html)（**正式前端**） |
| **原型** | [`docs/admin-prototype.html`](../docs/admin-prototype.html) 仅供需求/视觉参考，**不接线 API** |
| **路由** | FastAPI [`app/main.py`](app/main.py) 对 **`GET /`** 返回该文件（同源调用 `/api/*`） |
| **页面** | **总览**：劫持总开关、`/health`、规则统计、**VPN 摘要**；**BGP 管理**；**BGP 学习路由**；**逐跳规则**；**ARP 引流**；**VPN 出口**（隧道/策略/下发/ping） |
| **ARP 邻居恢复** | 删除 ARP 引流目标后，会尽力用「下一跳 MAC」发恢复 GARP（进程内需 **scapy** 与发 GARP 相同权限）；否则下游仍可能 `ip neigh show` 到旧 lladdr。关闭：`MTR_OP_ARP_RESTORE_NEIGH=0` |

直接双击打开 `static/index.html` 仅能看样式；调用 API 需通过上述 HTTP 服务（勿使用 `file://`）。

---

## 技术选型

| 类别 | 选型 |
|------|------|
| 后端 | Python 3 + **FastAPI** + **Uvicorn** |
| 持久化 | **SQLite**（`data.db`，路径由环境变量 `MTR_OP_DB` 指定） |
| 内核 | **iptables mangle**：转发的 **ICMP Time Exceeded** → NFQUEUE；**nft** 表 `ip mtr_te_snat` 为 TE SNAT 占位（[`nft_mtr_te.nft`](nft_mtr_te.nft)） |
| 用户态 | [`te_rewrite_nfqueue.py`](../scripts/te_rewrite_nfqueue.py) 按 **hop_replace_rules** 改写 TE **外层源 IP** 为 `forged_src` |

---

## 架构示意

```mermaid
flowchart LR
  subgraph ui [浏览器]
    StaticPage[static_index.html]
    Swagger[/docs]
  end
  subgraph linux200 [Linux_200]
    API[FastAPI]
    DB[(SQLite)]
    IPT[iptables_mangle_NFQUEUE]
    NFQ[te_rewrite_nfqueue]
  end
  StaticPage -->|同源_fetch| API
  Swagger --> API
  API --> DB
  API --> IPT
  IPT -->|NFQUEUE| NFQ
```

**hijack_enabled** 开启时由 `te_rewrite_sync` **先 bind NFQUEUE 再装 iptables**，并拉起 **`te_rewrite_nfqueue`**；关闭时清空映射、拆 NFQUEUE、停止守护。改 hop 规则默认 **SIGHUP 热加载**（不 pkill）。排障见 [`docs/MTR_TE_REWRITE.md`](../docs/MTR_TE_REWRITE.md)。

---

## REST 接口（已实现）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 存活检测 |
| `GET` | `/api/global` | `hijack_enabled` |
| `PUT` | `/api/global` | body：`{"hijack_enabled": true/false}`，TE 改写总开关（nft SNAT 占位 + iptables NFQUEUE + te_rewrite 守护） |
| `GET` | `/api/hop-rules` | 逐跳替换规则列表 |
| `POST` | `/api/hop-rules` | 新增规则 |
| `PATCH` | `/api/hop-rules/{id}` | 更新规则 |
| `DELETE` | `/api/hop-rules/{id}` | 删除规则 |
| `GET`/`PUT` | `/api/arp-spoof/settings` | ARP 引流总开关 |
| `GET`/`POST`/`PATCH`/`DELETE` | `/api/arp-spoof/targets` | 冒充网关条目；保存后会触发 BGP ipvlan 卫星收敛（`MTR_BGP_IPVLAN_AUTO`，默认开），带 `satellite_vrf` 的条目由对应 VRF 持有 /32，不再额外加到物理口主表 |
| `POST` | `/api/arp-spoof/satellite-vrfs/reconcile` | 按当前库 + 环境变量执行卫星 VRF 对齐（ipvlan L2 + **ip rule** + **nft DNAT**；含 legacy veth 摘要） |
| `POST` | `/api/bgp/ipvlan-satellites/reconcile` | 仅 ipvlan L2 收敛：`iv*`、VRF 路由、**ip rule**、**`mtr_bgp_sat_dnat`**（见 [BGP_SATELLITE_IP_RULE_AND_DNAT.md](../docs/BGP_SATELLITE_IP_RULE_AND_DNAT.md)） |
| `GET` | `/api/bgp/vrfs` | meta / 卫星配置 / 内核 **`ip link type vrf`** 中的 VRF 列表 |
| `POST` | `/api/bgp/instances` | 按需创建内核 VRF（GoBGP 按 VRF 懒启动 TX，不依赖 FRR） |
| `GET` | `/api/bgp/neighbors` | 从 **bgp-agent** 读邻居；与 SQLite meta 合并展示 |
| `POST` | `/api/bgp/neighbors` | 下发 **bgp-agent**（RR→RX，下游/卫星→TX）+ 写 meta；可选建内核 VRF / ipvlan |
| `PATCH` | `/api/bgp/neighbors/{vrf}/{neighbor_ip}` | 删后重建 Agent 邻居并更新 meta |
| `DELETE` | `/api/bgp/neighbors/{vrf}/{neighbor_ip}` | 从 Agent 移除（RR 除外）并删 meta |
| `POST` | `/api/bgp/neighbors/{vrf}/{neighbor_ip}/toggle` | body：`{"enabled": bool}` → Agent |
| `POST` | `/api/bgp/sync-from-frr` | 从 **bgp-agent** 合并邻居到库 + 预设角色（URL 保留兼容） |
| `GET` | `/api/vpn/summary` | VPN 隧道统计 |
| `GET`/`POST` | `/api/vpn/links` | 隧道列表 / 创建 |
| `GET`/`PATCH`/`DELETE` | `/api/vpn/links/{id}` | 单条隧道 |
| `GET`/`POST` | `/api/vpn/policies` | 策略列表 / 创建 |
| `PATCH`/`DELETE` | `/api/vpn/policies/{id}` | 单条策略 |
| `POST` | `/api/vpn/apply` | 幂等下发内核（GRE/OpenVPN/L2TP 包 + `ip rule`） |
| `POST` | `/api/vpn/ping` | VRF 内连通性探测 |
| `GET` | `/api/vpn/events` | 最近 VPN 事件 |

角色默认映射：环境变量 **`MTR_BGP_ROLE_MAP`**（`ip:role` 逗号分隔）；未设置时仅 **`139.159.43.249` → rr**、**`139.159.43.208` → downstream** 作 UI 提示。库中角色非 `unknown` 时视为 **手动** 覆盖。

**写入库的预设（OP 列表显示为「手动」）**：**`MTR_BGP_DB_PRESETS`**，格式 ``vrf:neighbor_ip:role`` 逗号分隔；**未设置时不自动写入任何邻居**（109 等现网在 `109/env` 中配置）。

**卫星 BGP 自动化（当前推荐）**：模块 **`app/bgp_ipvlan_reconcile.py`**，对应 [`docs/bgp-ipvlan-setup.md`](../docs/bgp-ipvlan-setup.md) 的 `ipvlan l2 + VRF` 架构；现网 **109** 另见 [`docs/BGP_SATELLITE_IP_RULE_AND_DNAT.md`](../docs/BGP_SATELLITE_IP_RULE_AND_DNAT.md)。开启 **`MTR_BGP_IPVLAN_AUTO=1`**（默认开）后，新增/修改 ARP 引流条目时，如果填写了 `satellite_vrf`（如 `vbgp13915943249`），OP 会在本机自动创建/维护 `iv{末字节}@<egress_iface>`、VRF 路由、**`ip rule from <冒充IP> lookup <卫星表>`**，并在 **`MTR_BGP_SAT_DNAT_AUTO=1`** 时写入 **`inet mtr_bgp_sat_dnat`**：下联口入站 **`daddr <冒充IP> tcp dport 179` → redirect 到该 VRF 的 TX 监听口**（非 RX `:179`）。BGP 新增邻居时，若 VRF 为 `vbgp*` 且未显式填 `source_ip`，会自动使用 ARP 条目的 `spoof_gateway_ip` 作为 TCP 源，并设置 **`bind_interface=iv*`**；卫星场景不下发 `ebgp-multihop`。补跑：`POST /api/arp-spoof/satellite-vrfs/reconcile` 或仓库 **`109/reconcile_satellite.py`**。

**旧卫星 VRF 自动化（veth/underlay）**：模块 **`app/satellite_vrf_assign.py`**。当 **`MTR_BGP_IPVLAN_AUTO`** 开启时，保存 ARP 条目后的旧 veth/dummy 收敛会被跳过，避免与新方案冲突。仅在显式关闭 `MTR_BGP_IPVLAN_AUTO=0` 时继续使用旧方案及其 **`MTR_AUTO_SATELLITE_VRF`**、**`MTR_SATELLITE_PHY_VRF`**、**`MTR_SATELLITE_BGP_TCP_SOURCE`** 等配置。

**BGP 页手输 VRF**：`POST /api/bgp/neighbors` / `POST /api/bgp/instances` 支持 **`create_kernel_vrf_if_missing`**（默认 true）与可选 **`kernel_rt_table`**；内核尚无该接口名时 OP 会 **`ip link add <name> type vrf table <id>`**。关闭自动建：**`MTR_BGP_AUTO_CREATE_KERNEL_VRF=0`**。表号范围：**`MTR_BGP_AUTO_VRF_TABLE_MIN`** / **`MTR_BGP_AUTO_VRF_TABLE_MAX`**（默认 30200–64999）。

---

## 故障：API 返回 404

日常代码更新按 **[`docs/部署.md`](../docs/部署.md)** 使用 **`tools/deploy_light.py`**。若存在 **systemd** `mtr-op.service`，也可改代码后 `systemctl restart mtr-op`（需与本机实际启动方式一致）。

新机全量使用 [`scripts/deploy_bgp_rxtx.py`](scripts/deploy_bgp_rxtx.py)。**`nft -f` 失败时仍启动 uvicorn**，便于先排查 API。

---

## 相关文件

- VPN 设计说明（仓库根 `docs/`）：[`VPN_EGRESS_DESIGN_NOTES.md`](../docs/VPN_EGRESS_DESIGN_NOTES.md)
- VPN API smoke：`python service/scripts/verify_vpn_api.py`（环境变量 **`MTR_OP_API`**，默认 `http://127.0.0.1:8808`）
- 操作手册：[`docs/OP_OPERATION_MANUAL.md`](../docs/OP_OPERATION_MANUAL.md)
- 部署说明：[`docs/部署.md`](../docs/部署.md)
- TE 改写 / 排障：[`docs/MTR_TE_REWRITE.md`](../docs/MTR_TE_REWRITE.md)
- 轻量部署：[`tools/deploy_light.py`](../tools/deploy_light.py)
- 全量部署：[`scripts/deploy_bgp_rxtx.py`](scripts/deploy_bgp_rxtx.py)
- 依赖：[`requirements.txt`](requirements.txt)
- 需求：[`docs/requirements-admin.md`](../docs/requirements-admin.md)（§2.5）；实验：[`step.md`](../step.md)（第十三节）
