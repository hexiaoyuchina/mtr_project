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

**日常发版**（不清库）：[`tools/deploy_light.py`](../tools/deploy_light.py)

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

实验室环境请用 [`200/README.md`](../200/README.md)。
