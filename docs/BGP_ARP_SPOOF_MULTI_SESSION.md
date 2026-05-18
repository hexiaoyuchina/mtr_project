# ARP 代答 + 多本机身份与同一对端建多条 BGP

> **现网 OP 主机**：`101.89.68.109`（原实验室文档中的 Linux 200 / `10.133.152.200`）。  
> **现网 BGP**：`LOCAL_AS=63199`，RR `139.159.43.249`，下游 `139.159.43.208` — 网口分工见 **[BGP_OP_NETWORK.md](./BGP_OP_NETWORK.md)**；控制面架构见 **[BGP_ARCHITECTURE.md](./BGP_ARCHITECTURE.md)**（GoBGP RX/TX + SQLite，非 FRR 会话）。  
> 部署见 [部署.md](./部署.md)、[BGP_RXTX_DEPLOYMENT.md](./BGP_RXTX_DEPLOYMENT.md)。  
> 下文 `10.133.152.*` 示例仅适用于 **VM 实验室**（Linux 200/201），勿直接套用到现网。

## 需求在协议里的含义

目标：**OP 主机（现网 `101.89.68.109`，实验室为 Linux 200）** 通过 ARP 引流在本机接口上挂多个「冒充网关 / 代答」IPv4，并以**不同本机源地址**与对端建立**多条 BGP 会话**。

在 **GoBGP Agent** 中，同一 **TX 实例（一个卫星 VRF）** 里，**每个对端 IPv4（`neighbor <ip>`）只能出现一次**。  
`local_address` / TCP 源只决定**本端身份**，**不能**在同一 VRF 内用「同一对端 IP + 两个不同源地址」配两条邻居（与旧版 FRR 单实例约束相同）。

因此：**多条 OP 记录 = 多个「邻居 IP」字段（对端用于 BGP 的地址）**；每条记录可配各自的 **TCP 源（update-source）**，与 ARP 代答的某个 IPv4 对齐。

---

## 多 VRF：各自一条会话连同一 Linux 201 地址（方案 3）

若 **每个卫星 VRF 对应一个 GoBGP TX 实例**，则 **不同 VRF 内可以对「同一个 201 的邻居 IP」各建一条会话**（Agent 按 VRF 隔离）。  
OP 已支持：

- **`GET /api/bgp/vrfs`**：合并 Agent 已有 VRF 与内核 **`ip link type vrf`**（`has_router_bgp: false` 表示尚无 TX 会话）。  
- **`POST /api/bgp/neighbors`**：经 OP 写 meta、做 ipvlan 收敛，并调用 **bgp-agent** 加邻居；可选 **`bgp_local_as`** / **`bgp_router_id`**。  
- **`POST /api/bgp/instances`**：仅创建内核 VRF 设备，不添加邻居。

各 VRF 内需保证到 201 的 **L3 可达**，且 **代答 `/32` 挂在该 VRF 的出接口**上（与 `arp_spoof_targets.egress_iface` 一致）。

### 做法 B：201 侧不改，仅在 Linux 200 扩多 VRF

若 **201 不能增地址**，仍要让 **四条会话的邻居 IP 都是 `10.133.152.204`**，只能在 **200 上为每条会话使用不同卫星 VRF**（每个 VRF 的 TX 各连 `10.133.152.204`，TCP 源为不同冒充 IP）。

要点：

1. **卫星 VRF**（如 `vbgp10133152250`）内需要 **本机 `/32`** 作为 BGP TCP 源；现网由 **ipvlan**（`iv250@ens192` 等）挂 `10.133.152.x/32`（见 `bgp_ipvlan_reconcile`）。  
2. **ARP 二层代答**仍建议由 OP 在 **现网口**（如 `ens192`，属 `vrf2102`）上加同名 `/32`，以便 GARP 与 201 的 `ip neigh` 一致；**同一 IPv4 可再出现在卫星 VRF 的 dummy 上**（不同 VRF 路由表隔离，内核允许）。  
3. 卫星 VRF 到 `10.133.152.204` 需 **经 `vrf2102` 转发**：仓库脚本 `scripts/linux200_multi_vrf_bgp_one_peer.sh` 为每个冒充末字节创建 **VRF + dummy + veth 钉到 `MTR_PHY_VRF`（默认 `vrf2102`）** 及 **host 路由 `/32 -> 204`**。在 200 上执行：  
   `bash scripts/linux200_multi_vrf_bgp_one_peer.sh setup`  
   撤销：`… teardown`。  
4. 然后 **OP → BGP 管理**：对每个 `vbgp*` 各新增邻居 **`10.133.152.204`**，**TCP 源** 填对应 `10.133.152.250`…`253`；脚本末尾会打印 `curl` 示例（`bgp_router_id` 建议与冒充 IP 一致以免冲突）。  
5. **自动化**：OP 内置 **`satellite_vrf_assign`**（`app/satellite_vrf_assign.py`）。在 Linux 200 上为 OP 配置环境变量 **`MTR_AUTO_SATELLITE_VRF=cidr`**（仅匹配 **`MTR_AUTO_SATELLITE_VRF_MATCH`**，默认 `10.133.152.0/24`）或 **`all`** / **`note`**（条目的 `note` 含 **`AUTOSAT`**）后，**保存 ARP 引流条目**（或调 **`POST /api/arp-spoof/satellite-vrfs/reconcile`**）即会为每个新 IP 自动创建 **`vbgp{IPv4 去点}`** 卫星 VRF（如 `10.133.152.233` → `vbgp10133152233`，见 `app/vrf_naming.py`）。需 **root**；删除 ARP 不会自动删 VRF。

---

## 推荐拓扑（Linux 201 上准备多个 BGP 会话地址）

在 **201** 上为 BGP 增加**第二个（或更多）可达 IPv4**，例如：

- 在面向 200 的网段接口上增加 secondary 地址；或  
- 增加 `loopback1` 等，地址如 `10.133.152.205/32` 或同网段第二地址，并保证 200 侧路由/ARP 可达；  
- 在 **201 对端** 对该地址配置 BGP 邻居（或让 200 TX **主动连**该地址），使 200 上表现为：

| OP / Agent 语义 | 邻居 IP（对端 BGP 地址） | TCP 源（200 上代答身份） |
|----------------|---------------------------|------------------------------------------|
| 第一条会话     | `10.133.152.204`（示例）   | `10.133.152.200`                         |
| 第二条会话     | `10.133.152.205`（示例）   | `10.133.152.250`                         |

**201 上**需有对应 `neighbor 10.133.152.200` / `neighbor 10.133.152.250` 或等价配置，指向 200 侧 AS；具体命令依 201 是 FRR 还是其它栈而定，原则为：**对端在 200 眼里是两个不同的 Neighbor 列 IP**。

---

## 在 OP（Linux 200）上操作

1. **ARP 引流**：为每个代答 IP 配置 `arp_spoof_targets`（冒充网关 + 出接口），保证 `ip addr` 上已有对应 `/32`（由 OP/守护进程维护）。  
2. **BGP 管理页**：  
   - **第一条**：VRF 选 `vrf2102`，**邻居 IP** 填 201 的第一个 BGP 地址，**TCP 源** 填第一个代答 IP。  
   - **第二条**：**邻居 IP 必须填 201 的第二个 BGP 地址**（与第一条不同），**TCP 源** 填第二个代答 IP。  
3. 若仅想**改**某条会话的本机源，不要「新增」第二条同邻居 IP；在列表里 **编辑** 该邻居，改 **TCP 源**。

重复「同一 VRF + 同一邻居 IP」会返回 **409** `neighbor_already_exists`，属预期行为。

---

## 验收：用 Agent API，不用 vtysh

**Linux 200 已停 FRR**，BGP 在 **bgp-agent（:9179）**。请用：

```bash
curl -s http://127.0.0.1:9179/api/neighbors | python3 -m json.tool
curl -s http://127.0.0.1:9179/api/peers/freeze-status | python3 -m json.tool
curl -s http://127.0.0.1:8808/api/bgp/neighbors | python3 -m json.tool
```

`ESTABLISHED` 表示会话已起；`ACTIVE` 且长期无报文时查 ipvlan、TCP 源、对端是否配了对应 `neighbor`。

**201 对端**若仍运行 FRR，可在 201 上用其自带 CLI 查看；**200 运维不以 vtysh 为准**。

---

## 现网：冒充 RR（249）在卫星 VRF 连下游（208），与真 RR 会话隔离

需求：**不能**在 default/主表用 249 与 RR 建连的同时又在主表用 249 连下游；应：

| 会话 | VRF | 邻居 | TCP 源 | 角色 | 数据面 |
|------|-----|------|--------|------|--------|
| 真 RR | `default` / `gobgp-rr` | `139.159.43.249` | `139.159.43.207` | RR | GoBGP **RX** |
| 冒充 RR 对下游 | `vbgp13915943249`（示例） | `139.159.43.208` | `139.159.43.249` | downstream | GoBGP **TX**（独立端口实例） |

操作顺序：

1. **ARP 引流**：冒充网关 `139.159.43.249`，`satellite_vrf` 填 `vbgp13915943249`（或留空由系统生成），**出接口** 选 **`eno1np0`**（下游与冒充 BGP 数据面；**勿**选 `enp59s0f0np0`，该口仅用于真 RR 会话 `207→249`）。
2. **卫星 VRF 收敛**（保存 ARP 或 `POST /api/arp-spoof/satellite-vrfs/reconcile`）：创建 ipvlan `iv249@eno1np0`、到 `MTR_BGP_IPVLAN_PEER_IP`（现网 `139.159.43.208`）的 VRF 路由，以及 `ip rule from 249 lookup <vrf表>`。真 RR 主表路由走 `MTR_BGP_RR_UPLINK_IFACE=enp59s0f0np0`，见 [BGP_OP_NETWORK.md](./BGP_OP_NETWORK.md)。
3. **BGP 管理**：VRF 选 `vbgp13915943249`，邻居 `139.159.43.208`，角色 **下游**，TCP 源 **249**；Agent 会对该邻居设置 `bind_interface=iv249`。
4. **208 侧**：须接受来自 `139.159.43.249` 的 BGP（或主动连该地址），与真 RR 路径独立。

环境变量（现网）：`MTR_BGP_IPVLAN_PEER_IP=139.159.43.208`，`MTR_BGP_IPVLAN_BASE_IFACE=eno1np0`，`MTR_BGP_RR_UPLINK_IFACE=enp59s0f0np0`，`MTR_SATELLITE_BGP_TCP_SOURCE=spoof`。

---

## 关联

- OP 前端：`service/static/index.html` BGP 管理（数据来自 bgp-agent + SQLite）。  
- API 重复新增：`POST /api/bgp/neighbors` 返回 409，`detail.code` 为 `neighbor_already_exists`。  
- ARP 与 `update-source`：`service/app/arp_spoof_assign.py`、`scripts/arp_spoof_daemon.py`。
