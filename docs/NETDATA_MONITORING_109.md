# 109 Netdata：怎么看监控

本文说明在 **101.89.68.109** 上安装 Netdata 后，打开 Web 界面如何查看 CPU、内存、网卡流量等指标。安装与保留策略见 [`109/deploy_netdata_109.py`](../109/deploy_netdata_109.py)、[`109/install_netdata_remote.sh`](../109/install_netdata_remote.sh)。

网口分工见 [BGP_OP_NETWORK.md](./BGP_OP_NETWORK.md)。

---

## 访问地址

| 项 | 值 |
|----|-----|
| URL | **http://101.89.68.109:19999** |
| 端口 | `19999/tcp`（公网需云安全组 / 上游防火墙放行） |
| 认证 | 默认**无密码**；任意来源可访问（见文末安全说明） |

本地 API 自检：

```bash
curl -fsS http://101.89.68.109:19999/api/v1/info
```

---

## 第一次打开：跳过登录

首次进入时，右侧常见 **「Welcome to Netdata」** 并要求 **Sign-in**。现网单机监控**不必注册**：

1. 在页面**最下方**找到小字链接：**「Skip and use the dashboard anonymously」**
2. 点击后进入主机仪表盘，才会出现 CPU、网络等实时曲线

若只点绿色 **Sign-in**，会停留在账号流程，看不到业务图表。

---

## 界面分区（底部四个按钮）

跳过后，页面底部通常有四个入口（左侧 **Database** 为默认页）：

| 按钮 | 用途 | 何时用 |
|------|------|--------|
| **Database** | 采集指标数量、dbengine 各 Tier 保留时间、磁盘占用 | 确认 Netdata 是否在写库、历史能看多久 |
| **System** | CPU、内存、负载、系统级汇总 | 看机器是否吃满 |
| **Modules** | 按插件分的指标（网络、磁盘、进程等） | **看网卡流量、磁盘 IO 的主要入口** |
| **Directories** | 目录 / 挂载点相关 | 看磁盘空间、路径级统计 |

**Database 页不是业务流量图**，只有存储统计；要看运行数据请切 **System** 或 **Modules**。

---

## 推荐查看顺序（109 现网）

### 1. 系统总览

- 底部 → **System**
- 关注：**CPU utilization**、**Memory**、**System load**

### 2. 网卡流量（BGP / 数据面）

- 底部 → **Modules** → **Networking**（或 Network interfaces）
- 按现场网卡名选曲线（与 `109/env.example` 一致）：

| 网卡 | 角色 |
|------|------|
| `enp59s0f0np0` | 上联 RR（207 ↔ 249） |
| `eno1np0` | 下联 / 卫星父口（209，对 208） |
| `enp59s0f1np1` | 管理 SSH / Web（101.89.68.109） |

看 **received / sent**（rx/tx）判断流量是否异常、是否打满带宽。

### 3. 磁盘

- **Modules** → **Disks** / **Mount points**
- 或 **Directories** 看挂载点空间

### 4. 相关进程（若有）

- **Modules** → **Applications** 或 **Processes**
- 可搜 `redis`、`bgp`、`gobgp`、`python` 等与 OP / Agent 相关的进程

---

## 图表操作

| 操作 | 效果 |
|------|------|
| **鼠标悬停**曲线 | 查看某一时刻数值 |
| **拖拽**时间轴 | 查看刚才一段时间的变化 |
| **点击**图表或标题 | 放大 / 进入更细的子图 |
| **时间范围**（如 1h / 6h / 12h / 1d） | 拉长窗口看历史；粒度越细 Tier 0，越长可能落到 Tier 1/2 |

页面会**自动刷新**（数秒级），默认是实时监控，不是静态报表。

---

## 历史数据保留（约 24 小时需求）

项目在 109 上通过 `/etc/netdata/conf.d/mtr-retention-1d.conf` 配置 **dbengine**，目标为本地历史约 **1 天**（Tier 0：`retention time = 1d`，`retention size = 2GiB`）。

**Database** 页中的 **Tier 0/1/2** 表可能显示安装包自带的更长 **Configured** 值（例如 14d）；以现场 **Effective Retention** 和磁盘占用为准。日常查看最近 **24 小时**时，在图表上将时间范围选到 **12h** 或 **1d** 即可。

核对远端配置：

```bash
cat /etc/netdata/conf.d/mtr-retention-1d.conf
systemctl status netdata
```

重新下发配置并重启：

```bash
python 109/deploy_netdata_109.py
```

---

## 安装与排错（简表）

| 现象 | 处理 |
|------|------|
| 打不开 `:19999` | 查云安全组、本机 `iptables`/`ufw`；`ss -lntp \| grep 19999` 应监听 `0.0.0.0:19999` |
| 只有 Welcome / Sign-in | 点 **Skip and use the dashboard anonymously** |
| 只有 Database、无曲线 | 切 **System** / **Modules**；等待 10～30 秒后刷新 |
| 曲线只有最近几分钟 | 拉大时间范围（1h / 1d） |
| 服务未运行 | SSH 到 109：`systemctl restart netdata`；日志 `journalctl -u netdata -f` |

安装脚本日志：`/root/netdata_install.log`（完成标志 `/root/netdata_install.done`）。

---

## 安全说明

- Web 配置为 `bind to = *`、`allow connections from = *`（见 `mtr-public-web.conf`），**无登录即可读监控**。
- 若需限制访问：在云安全组收紧 **19999** 源 IP，或修改 `allow connections from` 为办公网段；必要时再考虑 Netdata 账号 / 反向代理鉴权。

---

## 相关文件

| 文件 | 说明 |
|------|------|
| [`109/deploy_netdata_109.py`](../109/deploy_netdata_109.py) | 本机 SSH 触发安装 / 配置同步 |
| [`109/install_netdata_remote.sh`](../109/install_netdata_remote.sh) | 远端 kickstart + 保留 + 公网监听 |
| [`109/netdata-retention-1d.conf`](../109/netdata-retention-1d.conf) | 保留策略模板 |
| [`109/README.md`](../109/README.md) | 109 脚本索引 |
