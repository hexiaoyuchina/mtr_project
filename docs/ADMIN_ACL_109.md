# 109 管理面访问白名单（SSH / OP Web）

限制 **TCP 22（SSH）** 与 **OP 端口（默认 8808）** 的源地址；其它端口与 BGP 数据面不受影响。

## 默认允许网段

| CIDR |
|------|
| `101.251.211.168/29` |
| `101.251.214.176/28` |
| `106.120.247.120/29` |
| `101.251.204.16/29` |
| `164.52.12.80/29` |
| `101.251.255.176/29` |

另始终放行：`127.0.0.0/8`、`ct state established,related`。

## 实现

- nft 表：`inet mtr_admin_acl`
- 因 uvicorn 监听 `0.0.0.0:8808`，规则对**所有网卡入站**的 22/8808 生效（不仅 `enp59s0f1np1` 管理口）

## 下发

```bash
# 仓库根目录，已配置 109/env
python 109/apply_admin_acl.py
python 109/apply_admin_acl.py --check
python 109/apply_admin_acl.py --teardown   # 紧急放开
```

持久化：`/usr/local/sbin/mtr-admin-acl.sh`（开机需自行加入 cron/systemd，与 transit 脚本相同）。

## 环境变量

| 变量 | 说明 |
|------|------|
| `MTR_ADMIN_ACL_CIDRS` | 逗号分隔 CIDR，覆盖默认六段 |
| `MTR_OP_PORT` | OP 端口，默认 `8808` |
| `MTR_ADMIN_ACL_NFT` | 远端 nft 文件路径，默认 `$MTR_OP_REMOTE_DIR/nft_mtr_admin_acl.nft` |
| `MTR_ADMIN_ACL_PERSIST` | 开机重载脚本路径 |

## 注意

1. **下发前**确认当前 SSH 客户端 IP 落在白名单内，否则会锁死管理访问。
2. 与 `docs/BGP_OP_NETWORK.md` 管理口分工无关：数据面 BGP 端口（179 等）不在此表限制范围内。
3. 仓库内模板：`scripts/nft_mtr_admin_acl.nft`；现网以 `apply_admin_acl.py` 生成为准。
