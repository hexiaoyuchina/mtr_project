# 实验：冒充 `10.133.152.233`（与 `index.html` 同源 API）

管理端：`http://10.133.151.200:8808`  
Agent：`http://10.133.151.200:9179`

## 1. ARP 引流（对应 UI「保存」）

**开启总开关** — `PUT /api/arp-spoof/settings`

```bash
curl -s -X PUT http://127.0.0.1:8808/api/arp-spoof/settings \
  -H 'Content-Type: application/json' \
  -d '{"arp_spoof_enabled":true}'
```

**新增条目** — `POST /api/arp-spoof/targets`（与 `index.html` `saveArpEdit` 相同字段）

```bash
curl -s -X POST http://127.0.0.1:8808/api/arp-spoof/targets \
  -H 'Content-Type: application/json' \
  -d '{
    "spoof_gateway_ip": "10.133.152.233",
    "satellite_vrf": "vbgp10133152233",
    "egress_iface": "ens192",
    "enabled": true,
    "policy_mode": "gateway_only",
    "policy_cidrs": "",
    "note": "lab233-test"
  }'
```

保存后会自动触发 ipvlan / ARP reconcile（见 `main.py` `api_arp_spoof_targets_post`）。

**卫星 VRF** — `POST /api/arp-spoof/satellite-vrfs/reconcile`（快速添加按钮也会调）

```bash
curl -s -X POST http://127.0.0.1:8808/api/arp-spoof/satellite-vrfs/reconcile
```

### 验收（Linux 201）

```bash
ping -c 3 10.133.152.233
ip neigh show 10.133.152.233 dev ens192
# 期望 lladdr 为 Linux 200 ens192：00:50:56:af:97:a6
```

200 上应有 `iv233@ens192` 地址 `10.133.152.233/32`（ipvlan），不一定在 `ens192` 主地址上。

## 2. BGP 管理（对应 UI「新增邻居」）

**经 OP** — `POST /api/bgp/neighbors`（与 `addBgpNeighbor`）

```bash
curl -s -X POST http://127.0.0.1:8808/api/bgp/neighbors \
  -H 'Content-Type: application/json' \
  -d '{
    "vrf": "vbgp10133152233",
    "neighbor_ip": "10.133.152.204",
    "remote_as": 63199,
    "role": "downstream",
    "source_ip": "10.133.152.233",
    "bgp_local_as": 63199,
    "bgp_router_id": "10.133.152.233",
    "create_kernel_vrf_if_missing": true
  }'
```

若 `ipvlan-satellites/reconcile` 报 500，可改用 **Agent**（GoBGP TX 按 VRF 独立端口）：

```bash
curl -s -X POST http://127.0.0.1:9179/api/neighbors/add \
  -H 'Content-Type: application/json' \
  -d '{
    "vrf": "vbgp10133152233",
    "address": "10.133.152.204",
    "remote_as": 63199,
    "local_address": "10.133.152.233",
    "bind_interface": "iv233@ens192",
    "role": "downstream"
  }'
```

**Linux 201（对端 BGP）** 需接受来自冒充 IP 的会话（实验室 201 若仍跑 FRR，配置等价于 `neighbor 10.133.152.233 remote-as 63199`；**200 侧不以 vtysh 验收，以 bgp-agent 为准**）。

**卫星会话端口**：`vbgp10133152233` 的 TX 监听 **1792**（非 179），201 仍连 `:179` 时需在本机加 DNAT（实验室脚本 `remote-nft-dnat-233.sh`）：

```bash
nft add table inet bgp_sat_dnat
nft add chain inet bgp_sat_dnat prerouting '{ type nat hook prerouting priority -100; policy accept; }'
nft add rule inet bgp_sat_dnat prerouting ip daddr 10.133.152.233 tcp dport 179 redirect to :1792
```

### 验收（200 上，GoBGP Agent + OP）

```bash
# 邻居与会话（9179 bgp-agent）
curl -s http://127.0.0.1:9179/api/neighbors | python3 -m json.tool
curl -s http://127.0.0.1:9179/api/peers/freeze-status | python3 -m json.tool

# 合并展示（8808 OP，含 meta）
curl -s http://127.0.0.1:8808/api/bgp/neighbors | python3 -m json.tool

# 内核 / TCP
ip -br a show iv233@ens192
ip route show vrf vbgp10133152233
ss -tnp | grep -E '152\.233|1792|1836'
```

卫星 VRF 的 TX 监听端口非 179（如 `vbgp10133152233` 约 1836），以 `ss -lnp | grep bgp_agent` 为准。

## 3. 本次实测结果（2026-05-17）

| 项 | 结果 |
|----|------|
| 201 ping `10.133.152.233` | 通 |
| 201 ARP | `00:50:56:af:97:a6`（200 `ens192`） |
| Agent 下游 `vbgp10133152233` → `10.133.152.204` | **ESTABLISHED**（加 DNAT 后） |
| Agent `vbgp10133152233` → `10.133.152.204` | **ESTABLISHED**（ipvlan + 邻居齐全后） |

## 4. 已知问题（实验室 DB）

- `POST /api/arp-spoof/targets` 可能因 `arp_spoof_targets.created_at` NOT NULL 返回 500；可用 `200/remote-fix-arp-db.py` 写库后再 reconcile。
- 验收以 **`curl :9179/api/neighbors`** 与 **`/api/peers/freeze-status`** 为准，不要用 `vtysh`（200 已停 FRR，BGP 在 **bgp-agent**）。

参考：[BGP_ARP_SPOOF_MULTI_SESSION.md](../docs/BGP_ARP_SPOOF_MULTI_SESSION.md)、[bgp-ipvlan-setup.md](../docs/bgp-ipvlan-setup.md)
