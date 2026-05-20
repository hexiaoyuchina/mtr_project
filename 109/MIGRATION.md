# 现网 101.89.68.109 迁移与干净重建

从实验环境迁回或在新机上重建 OP + GoBGP Agent 的操作手册。  
架构见 [`docs/BGP_ARCHITECTURE.md`](../docs/BGP_ARCHITECTURE.md)；日常发版见 [`docs/部署.md`](../docs/部署.md)。

---

## 设备参数审核清单（部署前人工确认）

在运行 `deploy_fresh.py` 前，对照 [`env.example`](env.example) 与现场核对：

| 类别 | 项 | 文档默认 | 你的现场 |
|------|-----|----------|----------|
| SSH | 管理 IP | `101.89.68.109` | |
| SSH | 用户 / 密码 | `root` / **填 `109/env`** | |
| 数据面 | RR 对端 | `139.159.43.249` | |
| 数据面 | 本端 RX 地址 | `139.159.43.207`（`ROUTER_ID`） | |
| 数据面 | 下游对端 | `139.159.43.208` | |
| BGP | `LOCAL_AS` / `RR_AS` | `63199` | |
| 网卡 | 上联 RR | `enp59s0f0np0` | |
| 网卡 | 下游 / 卫星父口 | `eno1np0` | |
| 环境 | 下联冒充 249 挂 `/32` | `MTR_BGP_RR_SPOOF_IPVLAN_ADDR=1`（上下联隔离） | |
| 网卡 | 管理（仅 SSH/Web） | `enp59s0f1np1` | |
| 防火墙 | TCP 8808、对端 179 | 已放行 | |

**脚本不负责**在目标机上配置 IP 地址与静态路由；见 [`docs/BGP_OP_NETWORK.md`](../docs/BGP_OP_NETWORK.md)。

---

## 组件与端口

| 组件 | 端口 | systemd / 进程 |
|------|------|----------------|
| BGP Agent | 9179 | `bgp-agent.service` |
| MTR OP Web | 8808 | `nohup uvicorn`（全量脚本） |
| MTR NFQUEUE | — | `mtr_spoof_nfqueue.py` |

---

## 存储（无需手工建表）

| 存储 | 路径 | 说明 |
|------|------|------|
| SQLite | `/root/mtr_op/data.db` | OP 控制面；`init_schema()` 自动建表 |
| Redis | `localhost:6379` | Agent 热 RIB |
| RocksDB | `/var/lib/bgp_agent/rocksdb` | Agent 冷 RIB |

### SQLite 表（自动创建）

`global_config`、`hop_replace_rules`、`arp_spoof_settings`、`arp_spoof_targets`、`gateway_reply_settings`、`gateway_reply_policies`、`bgp_neighbor_meta`、`bgp_learned_routes`、`bgp_upstream_route_cache`、`bgp_sticky_frr`、`bgp_rib_sync_state`、`bgp_peer_snapshot`、`vpn_links`。

字段说明：[`docs/BGP_DATA_AND_API.md`](../docs/BGP_DATA_AND_API.md)。

---

## 软件版本与 apt 包

| 项 | 要求 |
|----|------|
| Go | ≥ 1.21（`remote-bootstrap.sh` 可装 1.21.13） |
| Python | 3 + venv；依赖见 `service/requirements.txt` |
| 系统包 | `redis-server`、`librocksdb-dev`、`nftables`、`python3-scapy`、`libnetfilter-queue-dev`、`iproute2` 等 |

`remote-bootstrap.sh` 对已安装组件打印 `SKIP`。

---

## 操作步骤

### 0. 开发机准备

```powershell
cd <仓库根目录>
pip install paramiko
copy 109\env.example 109\env
# 编辑 109\env：至少 MTR_OP_SSH_PASSWORD
```

预览（不连 SSH）：

```bash
python 109/deploy_fresh.py --dry-run
```

### 1. 干净重建（删代码与三库）

由 `deploy_fresh.py` 上传并执行 `remote-clean-fresh.sh`：

- 停 `bgp-agent`、uvicorn、NFQUEUE
- `rm -rf /root/mtr_op`
- 清 RocksDB、`redis-cli FLUSHDB`、删 nft 表

**保留旧配置**时不要执行本步；改用 `tools/deploy_light.py` 或 `deploy_fresh.py --skip-clean` 且 `MTR_OP_PRESERVE_DIR=1`。

### 2. 条件安装依赖

`remote-bootstrap.sh`（已装则 SKIP）。

### 3. 全量部署

```powershell
.\109\deploy.ps1
```

内部调用 [`service/scripts/deploy_bgp_rxtx.py`](../service/scripts/deploy_bgp_rxtx.py)（**不**使用 `200/deploy.py`）。

可选：

```bash
python 109/deploy_fresh.py --skip-clean          # 仅 bootstrap + 全量
python 109/deploy_fresh.py --skip-bootstrap    # 依赖已齐
python 109/deploy_fresh.py --skip-install       # 跳过 apt（映射 MTR_OP_SKIP_INSTALL）
```

### 4. 业务配置（干净库后必做）

Web：`http://101.89.68.109:8808/`

- 创建 RR 邻居（角色 **rr**）→ Agent `POST /api/rr/config`
- 创建下游邻居（`139.159.43.208`，卫星 VRF）
- ARP 引流：冒充 IP、`eno1np0` 出接口

顺序见 [`docs/BGP_OP_NETWORK.md`](../docs/BGP_OP_NETWORK.md)。

下联 MTR 对称转发（2110/2111、105.94 邻居）见 [`docs/MTR_DOWNSTREAM_TRANSIT_109.md`](../docs/MTR_DOWNSTREAM_TRANSIT_109.md)；部署后执行 `python 109/apply_downstream_transit.py`（**不**随 `deploy_fresh` 自动执行）。

### 5. 验收

```bash
python 109/verify.py
```

```bash
curl -s http://127.0.0.1:9179/health
curl -s http://127.0.0.1:8808/health
curl -s http://127.0.0.1:8808/api/gobgp/status
```

---

## 与实验室 200 的差异

| 项 | 现网 109 | 实验室 200 |
|----|----------|------------|
| 目录 | `109/` | `200/` |
| 部署入口 | `deploy_fresh.py` | `deploy.py` |
| RR 接口 | `enp59s0f0np0` | `ens224` / vrf2103 |
| 下游接口 | `eno1np0` | `ens192` |
| FRR | 不用 | `remote-restart.sh` 会停 FRR |
| Go overlay | 无 | `200/overlay/bgp_agent` |

---

## 排错

| 现象 | 查看 |
|------|------|
| Agent 起不来 | `journalctl -u bgp-agent -n 50` |
| Web 不通 | `/tmp/mtr_op.log` |
| NFQUEUE | `/tmp/mtr_spoof_nfqueue.log`、`/tmp/te_rewrite_nfqueue.log` |
| NFQUEUE 仍绑 ens192/ens224 | 确认 `MTR_TE_REWRITE_OIF/IIF` 或 `MTR_BGP_*`；`iptables -t mangle -S FORWARD \| grep NFQUEUE` 应为 eno1np0 / enp59s0f0np0 |
| 网口配错 | `BGP_OP_NETWORK.md` |

---

## 附录：日常发版

仅更新代码、**保留** `data.db`：

```powershell
$env:MTR_OP_HOST = "101.89.68.109"
$env:MTR_OP_SSH_PASSWORD = "<密码>"
python tools/deploy_light.py
```
