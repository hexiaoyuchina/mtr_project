# 现网 OP 主机网口与 BGP 拓扑

本文档描述 **Linux 200 / OP 主机**（管理 IP `101.89.68.109`）上各网口的**固定分工**。此前曾误将卫星 VRF / 下游 BGP 的父接口配在 `enp59s0f0np0`，导致与真 RR 会话争用同一二层域；现网以本文为准。

## 逻辑拓扑

```
                    ┌─────────────────┐
                    │  RR             │
                    │ 139.159.43.249  │
                    └────────┬────────┘
                             │ BGP（RX，本端 TCP 源 207）
                             │ 接口 enp59s0f0np0
                    ┌────────▼────────┐
                    │  OP / GoBGP     │
                    │  207/24         │
                    └────────┬────────┘
                             │ BGP（TX，冒充 249 等）
                             │ 接口 eno1np0（ipvlan / 卫星 VRF）
                    ┌────────▼────────┐
                    │  其他对端       │
                    │ 139.159.43.208  │
                    └─────────────────┘

        管理面（SSH / Web 8808）：enp59s0f1np1 → 101.89.68.109
```

**数据面两条路径必须分离：**

| 路径 | 接口 | 本端地址 | 对端 | GoBGP | 说明 |
|------|------|----------|------|-------|------|
| 上游 RR | `enp59s0f0np0` | `139.159.43.207/24` | `139.159.43.249` | **RX** | 真 RR 会话；TCP 源 **207**；VRF `default` / `gobgp-rr` |
| 下游 / 卫星 | `eno1np0` | 冒充 IP（如 **249**） | `139.159.43.208` | **TX** | ARP 引流 + 卫星 VRF；ipvlan 父口为 **eno1np0** |
| 管理 | `enp59s0f1np1` | `101.89.68.109` | — | — | **仅** SSH、OP Web；不参与 BGP 数据面 |

## 网口明细

### `enp59s0f0np0` — 与 RR 建 BGP

- 地址：`139.159.43.207/24`
- 用途：GoBGP **RX** 与 RR `139.159.43.249`（AS `63199`）建立会话
- Router ID / RR 的 `local_address`：`139.159.43.207`
- **不要**在此口上做下游冒充 IP 的 ipvlan 父接口

### `eno1np0` — 卫星 VRF、冒充 IP、与其他对端建 BGP

- 用途：ARP 代答、卫星 VRF（如 `vbgp13915943249`）、ipvlan（如 `iv249@eno1np0`）、GoBGP **TX** 连下游
- 现网下游邻居：`139.159.43.208`；冒充 RR 时 TCP 源为 `139.159.43.249`
- nft 将发往冒充 RR 地址 `:179` 的**入站**（`iifname eno1np0`）重定向到对应 TX 监听口

### `enp59s0f1np1` — 仅管理

- 地址：`101.89.68.109`
- 用途：SSH、`http://101.89.68.109:8808/`、部署探测
- **不要**把 BGP 邻居、ARP 引流出接口、ipvlan 父口配在此网卡上

## 现网 BGP 参数速查

| 项 | 值 |
|----|-----|
| `LOCAL_AS` | `63199` |
| RR | `139.159.43.249` |
| RR 本端（RX） | `139.159.43.207` @ `enp59s0f0np0` |
| 下游（TX） | `139.159.43.208` @ 卫星 VRF（父口 `eno1np0`） |
| 冒充 RR 连下游时 TCP 源 | `139.159.43.249` |

## 环境变量（`mtr-op.service` / 部署脚本）

```bash
# 卫星 ipvlan 父接口（下游、冒充 249）— 必须为 eno1np0
MTR_BGP_IPVLAN_BASE_IFACE=eno1np0
MTR_BGP_IPVLAN_PEER_IP=139.159.43.208
MTR_SATELLITE_PEER_IP=139.159.43.208
MTR_SATELLITE_BGP_TCP_SOURCE=spoof

# 真 RR 所在二层口（主表到 249 的主机路由等）— 必须为 enp59s0f0np0
MTR_BGP_RR_UPLINK_IFACE=enp59s0f0np0

# GoBGP / OP
ROUTER_ID=139.159.43.207
RR_ADDR=139.159.43.249
```

## 常见误配（勿再出现）

| 误配 | 后果 |
|------|------|
| `MTR_BGP_IPVLAN_BASE_IFACE=enp59s0f0np0` | ipvlan/ARP 与 RR 同口，249 地址冲突、207→249 建连失败 |
| ARP 引流「出接口」选 `enp59s0f0np0` 连下游 208 | 208 与 RR 不在同一物理路径时不可达 |
| `ip rule from 139.159.43.207 lookup` 卫星表 | RR 出站被导入卫星 VRF，无法连真 RR |
| 在 `iv249` 上挂 `249/32` 且与主表 RR 并存 | 内核视 249 为本机，207→249 不发 SYN |
| 把管理口 `enp59s0f1np1` 用于 BGP 或 ARP | 管理面与数据面混淆 |

## 操作顺序（冒充 RR 连 208）

1. **RR 会话**：BGP 管理 → 角色 **RR** → 邻居 `249`，TCP 源 **207**（走 RX，与 `enp59s0f0np0` 一致）。
2. **ARP 引流**：冒充 `139.159.43.249`，卫星 VRF `vbgp13915943249`，**出接口 `eno1np0`**。
3. **收敛**：保存 ARP 或 `POST /api/arp-spoof/satellite-vrfs/reconcile`（生成 `iv249@eno1np0` 等）。
4. **下游会话**：VRF 选卫星表，邻居 **208**，角色 **下游**，TCP 源 **249**。

详见 [BGP_ARP_SPOOF_MULTI_SESSION.md](./BGP_ARP_SPOOF_MULTI_SESSION.md)、[BGP_RXTX_DEPLOYMENT.md](./BGP_RXTX_DEPLOYMENT.md)。

## 关联文档

- [BGP_ARCHITECTURE.md](./BGP_ARCHITECTURE.md) — 双向学/存/冻/搬与 RX/TX
- [BGP_DATA_AND_API.md](./BGP_DATA_AND_API.md) — SQLite 表与 HTTP 接口
- [BGP_RXTX_DEPLOYMENT.md](./BGP_RXTX_DEPLOYMENT.md) — 部署与验收
- [BGP_ARP_SPOOF_MULTI_SESSION.md](./BGP_ARP_SPOOF_MULTI_SESSION.md) — 多会话与冒充 RR（内核/VRF）
