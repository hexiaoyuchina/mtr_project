# ARP 代答 + 多本机身份与同一对端建多条 BGP

> **现网 OP 主机**：`101.89.68.109`（原实验室文档中的 Linux 200 / `10.133.152.200`）。  
> **现网 BGP**：`LOCAL_AS=63199`，RR `139.159.43.249`，下游 `139.159.43.208` — 见 [部署.md](./部署.md)、[BGP_RXTX_DEPLOYMENT.md](./BGP_RXTX_DEPLOYMENT.md)。  
> 下文 `10.133.152.*` 示例仅适用于 **VM 实验室**（Linux 200/201），勿直接套用到现网。

## 需求在协议里的含义

目标：**OP 主机（现网 `101.89.68.109`，实验室为 Linux 200）** 通过 ARP 引流在本机接口上挂多个「冒充网关 / 代答」IPv4，并以**不同本机源地址**与对端建立**多条 BGP 会话**。

在 **FRR** 中，同一 `router bgp … vrf …` 实例里，**每个对端 IPv4（`neighbor <ip>`）只能出现一次**。  
`update-source` 只决定**本端 TCP 源**，**不能**用「同一对端邻居 IP + 两个不同 update-source」配置两条邻居。

因此：**多条 OP 记录 = 多个「邻居 IP」字段（对端用于 BGP 的地址）**；每条记录可配各自的 **TCP 源（update-source）**，与 ARP 代答的某个 IPv4 对齐。

---

## 多 VRF：各自一条会话连同一 Linux 201 地址（方案 3）

若 **每个 VRF 起一个 `router bgp … vrf …`**，则 **不同 VRF 内可以对「同一个 201 的邻居 IP」各配置一条** `neighbor`（FRR 按实例隔离）。  
OP 已支持：

- **`GET /api/bgp/vrfs`**：除 FRR 已有实例外，会并入 **`ip link type vrf`** 中**尚未** `router bgp` 的 VRF（`has_router_bgp: false`，下拉会标注「尚无 BGP」）。  
- **`POST /api/bgp/neighbors`**：若所选 VRF 尚无 BGP 实例，会按 **`bgp_local_as`**（或 `MTR_BGP_ENSURE_LOCAL_AS` / 与现有非 default 实例同 AS）**自动执行** `router bgp <AS> vrf <name>`；可选 **`bgp_router_id`** 或环境变量 **`MTR_BGP_ENSURE_ROUTER_ID`**。  
- **`POST /api/bgp/instances`**：仅建仓、不添加邻居。

各 VRF 内需保证到 201 的 **L3 可达**，且 **代答 `/32` 挂在该 VRF 的出接口**上（与 `arp_spoof_targets.egress_iface` 一致）。

### 做法 B：201 侧不改，仅在 Linux 200 扩多 VRF

若 **201 不能增地址**，仍要让 **四条会话的邻居 IP 都是 `10.133.152.204`**，只能在 **200 上为每条会话使用不同 VRF**（每个 `router bgp … vrf …` 里各有一条 `neighbor 10.133.152.204` + 各自的 `update-source`）。

要点：

1. **卫星 VRF**（如 `vbgp250`…`vbgp253`）内需要 **本机 `/32`** 作为 FRR 的 `update-source`，一般用 **dummy** 挂 `10.133.152.x/32`。  
2. **ARP 二层代答**仍建议由 OP 在 **现网口**（如 `ens192`，属 `vrf2102`）上加同名 `/32`，以便 GARP 与 201 的 `ip neigh` 一致；**同一 IPv4 可再出现在卫星 VRF 的 dummy 上**（不同 VRF 路由表隔离，内核允许）。  
3. 卫星 VRF 到 `10.133.152.204` 需 **经 `vrf2102` 转发**：仓库脚本 `scripts/linux200_multi_vrf_bgp_one_peer.sh` 为每个冒充末字节创建 **VRF + dummy + veth 钉到 `MTR_PHY_VRF`（默认 `vrf2102`）** 及 **host 路由 `/32 -> 204`**。在 200 上执行：  
   `bash scripts/linux200_multi_vrf_bgp_one_peer.sh setup`  
   撤销：`… teardown`。  
4. 然后 **OP → BGP 管理**：对每个 `vbgp*` 各新增邻居 **`10.133.152.204`**，**TCP 源** 填对应 `10.133.152.250`…`253`；脚本末尾会打印 `curl` 示例（`bgp_router_id` 建议与冒充 IP 一致以免冲突）。  
5. **自动化**：OP 内置 **`satellite_vrf_assign`**（`app/satellite_vrf_assign.py`）。在 Linux 200 上为 OP 配置环境变量 **`MTR_AUTO_SATELLITE_VRF=cidr`**（仅匹配 **`MTR_AUTO_SATELLITE_VRF_MATCH`**，默认 `10.133.152.0/24`）或 **`all`** / **`note`**（条目的 `note` 含 **`AUTOSAT`**）后，**保存 ARP 引流条目**（或调 **`POST /api/arp-spoof/satellite-vrfs/reconcile`**）即会为每个新 IP 自动创建 **`vbgp{末字节}`** 卫星 VRF（与手工脚本等价）。需 **root**；删除 ARP 不会自动删 VRF。

---

## 推荐拓扑（Linux 201 上准备多个 BGP 会话地址）

在 **201** 上为 BGP 增加**第二个（或更多）可达 IPv4**，例如：

- 在面向 200 的网段接口上增加 secondary 地址；或  
- 增加 `loopback1` 等，地址如 `10.133.152.205/32` 或同网段第二地址，并保证 200 侧路由/ARP 可达；  
- 在 **201 的 FRR** 里对该地址做 `neighbor`（或让 200 主动连该地址），使 200 上表现为：

| OP / FRR 语义 | 邻居 IP（对端 BGP 地址） | TCP 源 / update-source（200 上代答身份） |
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

## 与 `show bgp summary` 的对应关系

在 **201** 上 `Neighbor` 列会出现**多个**对端地址（200 的多个身份在 201 上表现为多个 Remote），每个对应 200 上一行邻居配置。  
在 **200** 上 `show bgp vrf vrf2102 summary` 的 `Neighbor` 列为 **201 的多个地址**，各行可对应不同的 `update-source`。

---

## 关联

- OP 前端说明：`service/static/index.html` BGP 页内「FRR 约束」段落。  
- API 重复新增：`POST /api/bgp/neighbors` 返回 409，`detail.code` 为 `neighbor_already_exists`。  
- ARP 与 `update-source`：`service/app/arp_spoof_assign.py`、`scripts/arp_spoof_daemon.py`。
