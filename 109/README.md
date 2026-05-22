# 现网 VR（`101.89.68.109`）

本目录集中 **现网 OP 主机** 的迁移、干净重建与验收脚本；与实验室 [`200/`](../200/) 分离，**不修改** `200/` 内文件。

| 角色 | 接口 | 地址 / 对端 |
|------|------|-------------|
| 管理 SSH / Web | `enp59s0f1np1` | `101.89.68.109:8808` |
| 上游 RR | `enp59s0f0np0` | 本端 `139.159.43.207` → RR `139.159.43.249` |
| 下游 / 卫星 | `eno1np0` | 对端 `139.159.43.208` |

网口分工见 [`docs/BGP_OP_NETWORK.md`](../docs/BGP_OP_NETWORK.md)。

---

## 审核设备信息（部署前）

1. 复制 [`env.example`](env.example) → **`env`**（勿提交 Git）
2. 核对 **IP、AS、网卡名** 是否与现场一致；**必填** `MTR_OP_SSH_PASSWORD`
3. 阅读 [`MIGRATION.md`](MIGRATION.md) §「设备参数审核清单」

---

## 一键部署（审核通过后）

```powershell
# 仓库根目录
pip install paramiko
.\109\deploy.ps1
```

或分步：

```bash
python 109/deploy_fresh.py      # 仅部署，可先 --dry-run
python 109/verify.py            # 验收
```

**日常发版**（不清库，推荐）：

```powershell
# 默认已走本机预编译上传（109/env：PREBUILT=1、BUILD_BGP_AGENT=0）
python tools/deploy_light.py
# 改了 Go 代码时先编译再部署：
python tools/bgp_agent_build.py
python tools/deploy_light.py
# 或一键：python tools/_deploy_once.py
```

### 补跑卫星收敛（ipvlan + ip rule + nft DNAT）

冒充 RR 连下游若缺 `ip rule` / `mtr_bgp_sat_dnat`，可手工（说明见 [`docs/BGP_SATELLITE_IP_RULE_AND_DNAT.md`](../docs/BGP_SATELLITE_IP_RULE_AND_DNAT.md)）：

```bash
python 109/reconcile_satellite.py
# 仅 vbgp13915943249 / 249：
python 109/reconcile_satellite.py --vrf vbgp13915943249 --spoof 139.159.43.249
```

默认会重建 bgp-agent 下游邻居（`bind_interface=iv249`）；仅收敛内核加 `--no-recycle-bgp`。

---

## 文件说明

| 文件 | 作用 |
|------|------|
| [`MIGRATION.md`](MIGRATION.md) | 完整迁移手册 |
| [`env.example`](env.example) | 环境变量模板 |
| `env` | 本地密码与覆盖项（自建） |
| [`remote-clean-fresh.sh`](remote-clean-fresh.sh) | 远端清代码与三库 |
| [`remote-bootstrap.sh`](remote-bootstrap.sh) | 条件 apt / Go / Redis |
| [`deploy_fresh.py`](deploy_fresh.py) | 编排 clean → bootstrap → `deploy_bgp_rxtx.py` |
| [`deploy.ps1`](deploy.ps1) | PowerShell 一键 |
| [`verify.py`](verify.py) | SSH/HTTP 验收 |
| [`reconcile_satellite.py`](reconcile_satellite.py) | 补跑 ipvlan / ip rule / 卫星 DNAT，可选回收 BGP |
| [`apply_downstream_transit.py`](apply_downstream_transit.py) | 下联 transit 规则（2110/2111 等） |
| [`cleanup_bgp_lab_presets.py`](cleanup_bgp_lab_presets.py) | 清理实验室 BGP 预设 |
| [`deploy_netdata_109.py`](deploy_netdata_109.py) | 安装 Netdata（可选） |
| [`docs/NETDATA_MONITORING_109.md`](../docs/NETDATA_MONITORING_109.md) | Netdata 安装后**怎么看监控**（Web 操作说明） |
| [`check_bgp_meta.py`](check_bgp_meta.py) / [`start_missing_services.py`](start_missing_services.py) | 巡检 / 拉起缺失服务 |

**仓库根 `tools/`**：`deploy_light.py`、`bgp_agent_build.py`、`_deploy_once.py`、`sync_bgp_agent.py`。

实验室环境请用 [`200/README.md`](../200/README.md)。
