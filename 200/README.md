# Linux 200 实验室（`10.133.151.200`）

本目录集中 **200 环境** 的部署与实验校验脚本；**不修改** 仓库根目录的 `tools/`、`service/`、`docs/` 等原文件。

## 网口与 BGP（与现网 RX/TX 架构映射）

| 角色 | 接口 | IP |
|------|------|-----|
| 管理 / SSH / Web | `ens160` | `10.133.151.200` |
| 下游（Linux 201） | `ens192` | `10.133.152.200` → 邻居 `10.133.152.204` |
| 上游 RR（ROS） | `ens224` / `vrf2103` | `10.133.153.200` → `10.133.153.204` |

| BGP | 值 |
|-----|-----|
| `LOCAL_AS` / 对端 AS | `63199` |
| `ROUTER_ID` | `10.133.153.200` |

## 部署

```powershell
# 仓库根目录
pip install paramiko
.\200\deploy.ps1
```

或：

```bash
python 200/deploy.py
python 200/reconcile.py   # 网络前提 + RR/下游邻居（部署后建议执行）
python 200/verify.py
```

- 上传 `service/` 与 `scripts/mtr_spoof_nfqueue.py`，**保留** 远端 `data.db`
- `overlay/bgp_agent/...` 仅覆盖实验室需要的补丁（如 `learned_routes.go` 编译修复）
- 复用 `tools/deploy_light.py` 上传逻辑与 `tools/bgp_agent_remote.py` 写 systemd
- 远端执行 `remote-network-prereq.sh`、`remote-restart.sh`（含完整 `MTR_BGP_*` 环境变量）

密码等见 `lab.env`（勿提交到公网 Git）。

## 实验校验

| 脚本 | 说明 |
|------|------|
| `verify.py` | Agent/OP 健康、AS、freeze-status、BGP TCP、ping |
| `remote-network-prereq.sh` | RR 源地址走 `vrf2103` 的 `ip rule`（部署时自动执行） |

远端等价手工验收见 [BGP_RXTX_DEPLOYMENT.md](../docs/BGP_RXTX_DEPLOYMENT.md) 第 6 节。

## 相关主机（不在此目录改配置）

| 主机 | 管理 IP | 说明 |
|------|---------|------|
| Linux 201 | `10.133.151.201` | 对端 BGP（实验室常为 FRR）；200 侧为 **GoBGP Agent** |
| RouterOS 210 | `10.133.151.210` | BGP 对 `10.133.153.200` AS `63199` |

## 实验脚本（API 与验收）

| 文件 | 作用 |
|------|------|
| `VERIFY-BGP.md` | **200 上 BGP 验收**（`curl :9179` / `:8808`，勿用 vtysh） |
| `API-test-arp233.md` | 冒充 `10.133.152.233` 的 curl 与验收说明（对齐 `index.html`） |
| `test_arp233_bgp.py` | 一键调 OP API + SSH 验收 |
| `remote-fix-arp-db.py` / `remote-bgp233.sh` | 写 ARP 库 + 触发 reconcile |
| `remote-agent-add-vbgp233.sh` | Agent 添加 `vbgp10133152233` 邻居 |
| `remote-nft-dnat-233.sh` | `10.133.152.233:179` → TX 监听口（仅当**对端主动连**冒充 IP 的 179 时需要） |

## 文件说明

| 文件 | 作用 |
|------|------|
| `lab.env` | 200 环境变量模板 |
| `deploy.py` / `deploy.ps1` | 部署入口 |
| `verify.py` | 验收入口 |
| `remote-restart.sh` | 远端 OP/NFQ 重启（含 lab 环境变量） |
| `remote-network-prereq.sh` | 上游 RR 路由前提 |
| `overlay/` | 仅上传到 200 的源码覆盖，不改仓库 `service/` |
