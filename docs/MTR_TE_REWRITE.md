# MTR / ICMP Time Exceeded 逐跳改写（路径 A）

现网 **仅保留路径 A**：`iptables mangle` 将转发的 **ICMP Time Exceeded（type 11）** 送入 **NFQUEUE**，用户态 **`te_rewrite_nfqueue.py`** 按规则把 **TE 外层 IPv4 源地址** 改为 `forged_src`。

已移除 **路径 B**（`mtr_spoof_nfqueue`、Echo 进队合成 TE）。勿再部署或运行旧脚本。

---

## 控制面与数据面

| 项 | 说明 |
|----|------|
| **总开关** | SQLite `global_config.hijack_enabled`；Web **总览**「劫持总开关」；`PUT /api/global` |
| **规则表** | `hop_replace_rules`：`match_cidr`、`forged_src`、`priority`、`enabled` |
| **映射文件** | 默认 `/tmp/mtr_te_map.env`（`MTR_TE_REWRITE_MAP_FILE` 可覆盖） |
| **守护进程** | `te_rewrite_nfqueue.py`（**不依赖 scapy**，用 struct 改 IP 与校验和） |
| **同步模块** | `service/app/te_rewrite_sync.py`（由 uvicorn 启动与 API 写库后触发） |
| **nft 占位** | 表 `ip mtr_te_snat`（[`nft_mtr_te.nft`](../service/nft_mtr_te.nft)）；**报表以 iptables + NFQUEUE 为准** |

**`hijack_enabled=true`**：先确保守护进程 **bind NFQUEUE**，再安装 iptables；失败则 **拆除 NFQUEUE**，避免 TE 被内核丢弃（mtr 全 `???` / timeout）。

**`hijack_enabled=false`**：清空映射、停止守护、**清除** mangle 中 NFQUEUE 规则（TE 由内核正常转发，**逐跳替换不生效**）。

---

## `match_cidr` 语义

与 [`story.md`](../story.md) §4.3 一致：

- 无 `/`：单主机，等价 `/32`。
- 带前缀：从所写起始 IP 起连续 `2^(32-前缀)` 个地址（与 `strict=False` 网络对齐不同）。
- 展开上限：`MTR_TE_REWRITE_MAX_EXPAND`（默认 4096）。

**`match_cidr` 须与真实 TE 报文的外层源 IP 一致**（MTR 在 traceroute 里显示的那一跳），不是内层 Echo 的地址。

---

## 109 下游 MTR 与接口

下联去程/回程策略路由见 [MTR_DOWNSTREAM_TRANSIT_109.md](./MTR_DOWNSTREAM_TRANSIT_109.md)。

| 环境变量 | 典型值（109） | 含义 |
|----------|---------------|------|
| `MTR_TE_REWRITE_SCRIPT` | `/root/mtr_op/te_rewrite_nfqueue.py` | 守护脚本路径 |
| `MTR_TE_REWRITE_OIF` | `eno1np0` | FORWARD：从下联出的 TE 进队 |
| `MTR_TE_REWRITE_IIF` | `enp59s0f0np0` | FORWARD：从上联进、下联出的 TE 进队 |
| `MTR_TE_REWRITE_OUTPUT` | 默认开 | OUTPUT：本机生成的 TE 也进队；`0` 关闭 |

**勿**在 `eno1np0` 上遗留诊断用的 `105.94/32` 等主机路由，否则会干扰去程/回程。

---

## 日常部署

见 [部署.md](./部署.md)。要点：

```powershell
# 全量（含 bgp-agent）
python tools/deploy_light.py

# 仅 OP / TE / 前端（不改 bgp-agent）
python tools/deploy_light.py --op-only
# 或：$env:MTR_DEPLOY_OP_ONLY = "1"
```

上传：`service/` + `scripts/te_rewrite_nfqueue.py` → `/root/mtr_op/`。

重启 uvicorn 后，`te_rewrite_sync` 按 **`hijack_enabled`** 决定是否装 NFQUEUE 与拉起守护进程。

---

## 增删改与 API（路径 A）

| 操作 | API | 落库后同步 |
|------|-----|------------|
| 列表 | `GET /api/hop-rules` | — |
| 新增 | `POST /api/hop-rules` | 写 `/tmp/mtr_te_map.env` → **SIGHUP** 热加载 → 失败则冷启动 `te_rewrite_nfqueue` |
| 修改 | `PATCH /api/hop-rules/{id}`（含启用/停用） | 同上 |
| 删除 | `DELETE /api/hop-rules/{id}` | 从映射移除对应 host → 同上 |
| 总开关 | `PUT /api/global` `hijack_enabled` | 开：bind + iptables；关：清映射、拆 NFQUEUE |

Web [`service/static/index.html`](../service/static/index.html) 与上述 API 一致。`enabled=false` 的规则**不**进入映射。

## 改规则后的行为

- **增删改 hop 规则**：默认 **SIGHUP** 热加载（**不 pkill**）；日志须出现 `reload rules=…` 且含新 `forged_src`，否则自动冷启动。
- **总开关切换 / uvicorn 冷启动**：可能 **冷启动**守护进程；须遵守 **先 bind 再装 iptables**。
- 本机开发可设 **`MTR_TE_REWRITE_SKIP_SYNC=1`** 跳过同步。

---

## 验收命令（SSH 到 109）

```bash
# 总开关与进程
curl -s http://127.0.0.1:8808/api/global
pgrep -af te_rewrite_nfqueue

# NFQUEUE 是否有人监听（应含队列号，如 0）
cat /proc/net/netfilter/nfnetlink_queue

# mangle 规则（应为 icmp-type 11，非 echo-request）
iptables -t mangle -S FORWARD
iptables -t mangle -S OUTPUT

# 映射与日志
head -c 500 /tmp/mtr_te_map.env
tail -30 /tmp/te_rewrite_nfqueue.log
```

下联抓包（示例）：TE **外层源** 应变为规则中的 `forged_src`（如 `100.100.100.100`）。

---

## 常见故障

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| mtr **全 timeout / ???** | NFQUEUE 已装但 **无人 bind**；或守护启动失败 | 看 `/tmp/te_rewrite_nfqueue.log`；`cat /proc/net/netfilter/nfnetlink_queue`；确认 `hijack_enabled` 与进程存活；必要时关总开关或 API 触发 sync 以 **clear NFQUEUE** |
| 关总开关后仍 timeout | 历史规则用 **`time-exceeded`** 安装、删除脚本只删 **`11`**，删不干净 | 升级 `te_rewrite_sync` 后重启 uvicorn，或手工 `iptables -t mangle -D` 残留 NFQUEUE 行 |
| 有跳但 **规则不生效** | `hijack_enabled=false`；`match_cidr` 与真实 TE 外层源不符；TE 未从 `MTR_TE_REWRITE_OIF` 出 | 开总开关；改规则；查 [MTR_DOWNSTREAM_TRANSIT_109.md](./MTR_DOWNSTREAM_TRANSIT_109.md) |
| 守护 **D 状态 / 起不来** | 旧版依赖 **scapy** 在 109 上 import 卡死 | 使用当前 **无 scapy** 的 `te_rewrite_nfqueue.py` + `deploy_light.py` |
| 改规则瞬间闪断 | 旧版 **pkill** 冷启动 | 使用带 **SIGHUP 热加载** 的 `te_rewrite_sync.py` |
| **库/UI 已改 forged_src，mtr 仍显示旧 IP** | 旧版 `te_rewrite_nfqueue` 在 **SIGHUP** 时仍读进程内 **`MTR_TE_REWRITE_MAP` 环境变量**（启动快照），不读已更新的 `/tmp/mtr_te_map.env` | 升级 `scripts/te_rewrite_nfqueue.py` 后重启 uvicorn，或 `kill -HUP $(pgrep -f te_rewrite_nfqueue)`；日志应出现 `reload rules=…200.200.200.200` |
| 与旧路径冲突 | 仍运行 **`mtr_spoof_nfqueue`** | `pkill -f mtr_spoof_nfqueue`；`nft delete table inet mtr_spoof` |

回程/邻居类问题（2111、`105.94` neigh）见 [MTR_DOWNSTREAM_TRANSIT_109.md](./MTR_DOWNSTREAM_TRANSIT_109.md)。

---

## 紧急恢复（仅排障）

若需先恢复 **可出跳、不改写** 的 mtr：

1. Web 或 API 将 **`hijack_enabled`** 设为 `false`。
2. 确认 mangle 无 NFQUEUE：`iptables -t mangle -S FORWARD | grep -i NFQUEUE` 应为空。
3. 再查下游 transit / 邻居脚本。

勿在生产中长期「只关开关、不重启 uvicorn」且遗留旧版删不掉的 NFQUEUE 规则。

---

## 关联文档

- [部署.md](./部署.md) — 发版与重启
- [MTR_DOWNSTREAM_TRANSIT_109.md](./MTR_DOWNSTREAM_TRANSIT_109.md) — 109 去程/回程
- [service/README.md](../service/README.md) — OP API
- [story.md](../story.md) — 业务与 ARP 并存说明
