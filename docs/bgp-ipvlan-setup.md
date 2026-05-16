# BGP 建立

> **历史实验室文档**（`10.133.152.*` / Linux 200–201）。  
> **现网**：`101.89.68.109`、`LOCAL_AS=63199`、`RR=139.159.43.249`、`下游=139.159.43.208` — 见 [部署.md](./部署.md)、[BGP_RXTX_DEPLOYMENT.md](./BGP_RXTX_DEPLOYMENT.md)。

## 目标效果

在不修改 Linux 201 FRR 配置的前提下，只调整 Linux 200，使 Linux 201 上已有的 5 个 BGP 邻居全部建立成功：

- `10.133.152.200`
- `10.133.152.250`
- `10.133.152.251`
- `10.133.152.252`
- `10.133.152.253`

Linux 201 固定配置如下：

```text
router bgp 65201
 bgp router-id 10.133.152.204
 neighbor 10.133.152.200 remote-as 65200
 neighbor 10.133.152.250 remote-as 65200
 neighbor 10.133.152.250 description vbgp250
 neighbor 10.133.152.251 remote-as 65200
 neighbor 10.133.152.251 description vbgp251
 neighbor 10.133.152.252 remote-as 65200
 neighbor 10.133.152.252 description vbgp252
 neighbor 10.133.152.253 remote-as 65200
 neighbor 10.133.152.253 description vbgp253
!
line vty
!
```

最终效果是 Linux 201 的 `show bgp ipv4 unicast summary` 中 5 个邻居均有 `Up/Down` 时间，不再处于 `Active` 或 `Idle`。

## 最终技术架构

Linux 201 不做任何改动，仍然在默认 VRF 中以 `10.133.152.204` 和 Linux 200 建立 eBGP。

Linux 200 侧采用如下结构：

- 默认 VRF 使用 `10.133.152.200/24` 直接通过 `ens192` 与 Linux 201 的 `10.133.152.204` 建立 BGP。
- `vbgp250` 使用 `iv250@ens192`，地址为 `10.133.152.250/32`。
- `vbgp251` 使用 `iv251@ens192`，地址为 `10.133.152.251/32`。
- `vbgp252` 使用 `iv252@ens192`，地址为 `10.133.152.252/32`。
- `vbgp253` 使用 `iv253@ens192`，地址为 `10.133.152.253/32`。

其中 `iv250-iv253` 是基于 `ens192` 创建的 `ipvlan l2` 子接口，并分别加入对应的 Linux VRF。这样每个 VRF 都能直接通过 `ens192` 所在二层网络访问 `10.133.152.204`。

逻辑关系：

```text
Linux 201
10.133.152.204/24
ens192
  |
  | L2
  |
Linux 200 ens192
  |-- default VRF: 10.133.152.200/24
  |-- iv250@ens192 -> vbgp250: 10.133.152.250/32
  |-- iv251@ens192 -> vbgp251: 10.133.152.251/32
  |-- iv252@ens192 -> vbgp252: 10.133.152.252/32
  |-- iv253@ens192 -> vbgp253: 10.133.152.253/32
```

## 以前失败的核心原因

以前失败不是单纯因为 FRR 配置缺失，而是 Linux 200 侧的源地址、VRF、本地路由和出接口没有统一。

主要问题：

- `10.133.152.25x` 虽然被用作 BGP `update-source`，但地址曾经挂在 dummy、`ens192` 或错误路由域中，FRR 在对应 VRF 内无法稳定绑定和出包。
- `vbgp250-253` 的 VRF 路由表缺少到 `10.133.152.204` 的正确路径，FRR 邻居详情中出现 `No path to specified Neighbor`。
- 部分 `ip rule` 把 `from 10.133.152.25x` 的流量提前导向 main 表，绕过 Linux VRF 的 `l3mdev` 规则，导致流量走错出口。
- 主表里同时存在 `ens161` 和 `ens192` 的 `10.133.152.0/24` 直连路由，错误策略路由会让 BGP 流量被带到错误接口。
- dummy + veth + policy route 的模拟方式过于复杂，地址归属和回程路径容易不一致。

最终改成 `ipvlan l2 + VRF` 后，每个 BGP 源 IP 都真实存在于对应 VRF 内，并直接接入 `ens192` 所在二层网络，因此能匹配 Linux 201 的固定邻居配置。

## Linux 200 实施步骤

以下操作只在 Linux 200 执行。

OP 后端已补齐自动化后，日常新增一组 BGP 源 IP 的推荐流程是：

1. 在 ARP 引流中新增启用条目，`spoof_gateway_ip` 填 `10.133.152.x`，`egress_iface` 填 `ens192`，`satellite_vrf` 填 `vbgpx`。
2. 在 BGP 管理中新增邻居，`vrf` 选择或填写同一个 `vbgpx`，`neighbor_ip` 填 Linux 201 的 `10.133.152.204`，`remote_as` 填 `65201`，`source_ip` 可留空。
3. 后端会自动创建/维护 `ivx@ens192`、VRF 路由、FRR `update-source 10.133.152.x`，并清理干扰的 `ip rule`。

相关环境变量：

```text
MTR_BGP_IPVLAN_AUTO=1
MTR_BGP_IPVLAN_BASE_IFACE=ens192
MTR_BGP_IPVLAN_PEER_IP=10.133.152.204
```

也可以手工触发后端收敛：

```bash
curl -sS -X POST http://127.0.0.1:8808/api/bgp/ipvlan-satellites/reconcile
```

### 1. 放宽反向路径过滤

```bash
sysctl -w net.ipv4.conf.all.rp_filter=0
sysctl -w net.ipv4.conf.default.rp_filter=0
sysctl -w net.ipv4.conf.ens192.rp_filter=0
sysctl -w net.ipv4.tcp_l3mdev_accept=1
sysctl -w net.ipv4.udp_l3mdev_accept=1
```

### 2. 配置默认 VRF 的 BGP 邻居

确保 `10.133.152.200` 从 `ens192` 到 `10.133.152.204`：

```bash
ip route replace 10.133.152.204/32 dev ens192 src 10.133.152.200
```

FRR 配置：

```bash
vtysh -c 'configure terminal' \
  -c 'router bgp 65200' \
  -c 'bgp router-id 10.133.152.200' \
  -c 'neighbor 10.133.152.204 remote-as 65201' \
  -c 'neighbor 10.133.152.204 update-source 10.133.152.200' \
  -c 'address-family ipv4 unicast' \
  -c 'neighbor 10.133.152.204 activate' \
  -c 'exit-address-family' \
  -c 'end' \
  -c 'write memory'
```

### 3. 清理干扰 VRF 的策略路由

删除把 `10.133.152.250-253` 提前导向 main 表或错误表的规则：

```bash
for L in 250 251 252 253; do
  for T in main 30450 30291 30292 30293 30250 30251 30252 30253; do
    while ip rule del from 10.133.152.${L}/32 lookup $T 2>/dev/null; do :; done
    while ip rule del to 10.133.152.${L}/32 lookup $T 2>/dev/null; do :; done
  done
done
```

保留 Linux VRF 默认需要的 `l3mdev` 规则：

```text
1000: from all lookup [l3mdev-table]
```

### 4. 创建 ipvlan 并加入对应 VRF

```bash
for L in 250 251 252 253; do
  VRF=vbgp${L}
  IV=iv${L}

  ip link del ${IV} 2>/dev/null || true
  ip link add link ens192 name ${IV} type ipvlan mode l2
  ip link set ${IV} master ${VRF}
  ip addr add 10.133.152.${L}/32 dev ${IV}
  ip link set ${IV} up

  sysctl -w net.ipv4.conf.${IV}.rp_filter=0 >/dev/null 2>&1 || true

  ip route replace vrf ${VRF} 10.133.152.204/32 dev ${IV} src 10.133.152.${L}
  ip route replace vrf ${VRF} 10.133.152.0/24 dev ${IV} src 10.133.152.${L}
done
```

### 5. 配置 Linux 200 的 VRF BGP

```bash
vtysh -c 'configure terminal' \
  -c 'router bgp 65200 vrf vbgp250' \
  -c 'bgp router-id 10.133.152.250' \
  -c 'neighbor 10.133.152.204 remote-as 65201' \
  -c 'neighbor 10.133.152.204 update-source 10.133.152.250' \
  -c 'address-family ipv4 unicast' \
  -c 'neighbor 10.133.152.204 activate' \
  -c 'exit-address-family' \
  -c 'router bgp 65200 vrf vbgp251' \
  -c 'bgp router-id 10.133.152.251' \
  -c 'neighbor 10.133.152.204 remote-as 65201' \
  -c 'neighbor 10.133.152.204 update-source 10.133.152.251' \
  -c 'address-family ipv4 unicast' \
  -c 'neighbor 10.133.152.204 activate' \
  -c 'exit-address-family' \
  -c 'router bgp 65200 vrf vbgp252' \
  -c 'bgp router-id 10.133.152.252' \
  -c 'neighbor 10.133.152.204 remote-as 65201' \
  -c 'neighbor 10.133.152.204 update-source 10.133.152.252' \
  -c 'address-family ipv4 unicast' \
  -c 'neighbor 10.133.152.204 activate' \
  -c 'exit-address-family' \
  -c 'router bgp 65200 vrf vbgp253' \
  -c 'bgp router-id 10.133.152.253' \
  -c 'neighbor 10.133.152.204 remote-as 65201' \
  -c 'neighbor 10.133.152.204 update-source 10.133.152.253' \
  -c 'address-family ipv4 unicast' \
  -c 'neighbor 10.133.152.204 activate' \
  -c 'exit-address-family' \
  -c 'end' \
  -c 'write memory'
```

### 6. 重置 BGP 连接

```bash
vtysh -c 'clear ip bgp *'

for L in 250 251 252 253; do
  vtysh -c "clear bgp vrf vbgp${L} *"
done
```

如果部分邻居还在等待重试，可以对 VRF 邻居做一次 shutdown/no shutdown：

```bash
for L in 250 251 252 253; do
  vtysh -c "configure terminal" \
    -c "router bgp 65200 vrf vbgp${L}" \
    -c "neighbor 10.133.152.204 shutdown" \
    -c "end"
done

sleep 2

for L in 250 251 252 253; do
  vtysh -c "configure terminal" \
    -c "router bgp 65200 vrf vbgp${L}" \
    -c "no neighbor 10.133.152.204 shutdown" \
    -c "end"
done
```

## 验证命令

### Linux 200 验证

查看默认 VRF BGP：

```bash
vtysh -c 'show bgp ipv4 unicast summary'
```

查看每个 BGP VRF：

```bash
for L in 250 251 252 253; do
  echo "### vbgp${L}"
  vtysh -c "show bgp vrf vbgp${L} ipv4 unicast summary"
done
```

查看 TCP 179 是否 Established：

```bash
ss -tnp '( dport = :179 or sport = :179 )' | grep 10.133.152
```

预期能看到类似：

```text
ESTAB 10.133.152.200:xxxxx        10.133.152.204:179
ESTAB 10.133.152.250%vbgp250:179  10.133.152.204:xxxxx
ESTAB 10.133.152.251%vbgp251:179  10.133.152.204:xxxxx
ESTAB 10.133.152.252%vbgp252:179  10.133.152.204:xxxxx
ESTAB 10.133.152.253%vbgp253:179  10.133.152.204:xxxxx
```

检查 VRF 内到邻居的路由：

```bash
for L in 250 251 252 253; do
  ip vrf exec vbgp${L} ip route get 10.133.152.204 from 10.133.152.${L}
done
```

### Linux 201 验证

```bash
vtysh -c 'show bgp ipv4 unicast summary'
```

预期 5 个邻居都有 `Up/Down` 时间：

```text
Neighbor        V     AS   MsgRcvd MsgSent  Up/Down State/PfxRcd
10.133.152.200  4  65200   ...     ...      00:xx:xx          0
10.133.152.250  4  65200   ...     ...      00:xx:xx          0
10.133.152.251  4  65200   ...     ...      00:xx:xx          0
10.133.152.252  4  65200   ...     ...      00:xx:xx          0
10.133.152.253  4  65200   ...     ...      00:xx:xx          0
```

确认 Linux 201 配置没有变化：

```bash
vtysh -c 'show running-config' | sed -n '/router bgp 65201/,/^line vty/p'
```

## 关于 `(Policy)` 的说明

Linux 200 的 FRR summary 中可能显示：

```text
State/PfxRcd   PfxSnt
(Policy)       (Policy)
```

这表示 FRR 没有配置明确的入/出策略，前缀收发被策略限制。它不代表 BGP 邻居没有建立。

判断邻居是否建立，应以以下信息为准：

- Linux 201 summary 中邻居有 `Up/Down` 时间。
- Linux 200 summary 中邻居有 `Up/Down` 时间。
- `ss` 能看到对应 TCP 179 连接为 `ESTAB`。

## 注意事项

- 不要修改 Linux 201 的 FRR 配置。
- 不要再使用会把 `from 10.133.152.25x` 提前导向 main 表的策略路由规则。
- 不要让 `10.133.152.25x` 同时以错误方式残留在 main 表接口上，否则可能再次出现本地路由和 VRF 路由冲突。
- 如果系统重启，需要确认 `iv250-iv253`、VRF 路由、sysctl 和 FRR 配置是否已持久化。
- 如果后续需要收发业务前缀，再单独补充 FRR 的 route-map 或 policy 配置。
