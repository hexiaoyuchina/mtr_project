# 静态路由（主机 ip route）

OP 提供可编辑的静态路由条目，通过 `ip route replace` 写入 **main**、**策略表（table）** 或 **卫星 VRF（vrf）**，并支持标记 **跨 VRF** 下一跳解析域。

## 与其它机制的分工

| 机制 | 维护方 | 说明 |
|------|--------|------|
| 卫星 `peer/32`（如 208） | ARP + `bgp_ipvlan_reconcile` | 随 ARP 条目自动写入 VRF，勿在静态路由重复配置 |
| **OP 静态路由表** | 本功能（**唯一控制面**） | `ip route` 的增删改以库为准：启用→`replace`；停用/删除→`del` |
| 下联 Transit `ip rule` + 脚本 | `109/apply_downstream_transit.py`（可选） | **勿与 OP 重复写同表同前缀**；若已用 OP 管理 2110/2111 路由，应停脚本或 `--teardown`，否则脚本会覆盖 OP 停用效果 |
| 用户其它手工 `ip route` | 运维自行 | 非 OP 管理；对账状态可能为 `disabled_leak` |

## 安装域

- **main**：`ip route replace <dst> …`
- **table**：`ip route replace table <id> <dst> …`（如 2110、2111）
- **vrf**：`ip route replace vrf <vbgp…> <dst> …`

## 跨 VRF

勾选后需指定 **下一跳域**（main / table / vrf）及 **下一跳标记**。表示该条路由的可达性在另一 FIB 中解析（典型：卫星 VRF 内 `0.0.0.0/0 via 249 dev enp59`，上联在 main 表）。

弹窗与列表会显示 **命令预览**；应用前请用 **批量探测**（`ip route get` / `ip vrf exec`）核对。

**探测说明**：对 **main/VRF** 执行 `ip route get <IP> [from 源]`；对 **table** 作用域，因 `ip route get` **不支持** `table` 参数，会按 `ip rule` 与路由上的 `egress_iface`/`pref_src` 拼 `iif`/`from`（109 下联场景常用 `iif eno1np0` + 客户端网段源地址）。**0.0.0.0/0** 用 `8.8.8.8`（或 `MTR_STATIC_ROUTE_PROBE_DST`）作探测 IP；仍失败时回退为 `ip route show table N` 的 LPM 匹配说明。

## API

| 方法 | 路径 |
|------|------|
| GET | `/api/static-routes/scopes` |
| GET | `/api/static-routes`（默认仅库 + 下发状态文件，快） |
| GET | `/api/static-routes?reconcile=1` | 逐条查内核 FIB（慢，大表慎用） |
| GET | `/api/static-routes/scopes?db_only=1` | 下拉选项仅库，不扫 `ip rule` |
| POST/PATCH/DELETE | `/api/static-routes` |
| POST | `/api/static-routes/apply` |
| POST | `/api/static-routes/probe` |

## 同步语义（OP 独占）

- **保存 / 编辑**：写 SQLite 并立即 `ip route replace`（状态 **已下发**）。
- **列表停用**：`ip route del`，库 `enabled=0`（状态 **已停用**）。
- **列表启用**：再次 `replace`，库 `enabled=1`（**已下发**）。
- **删除**：删库并删内核（默认）。
- **对账** `sync_state`：运行中条目为 `applied` / `stale` / `missing`；`enabled=0` 为 **已停用**（`stopped`）。

## 环境变量

- `MTR_STATIC_ROUTE_DRY_RUN=1`：只打印命令不执行
- `MTR_STATIC_ROUTE_AUTO_APPLY`：已废弃语义（保存恒写内核）；可忽略。

## 109 验收示例

```text
vrf vbgp13915943247
  dst: 139.159.43.208/32
  dev: iv247
  src: 139.159.43.247
```

跨 VRF 默认路由走上联时：`install_scope=vrf`，`cross_vrf=1`，`nexthop_scope=main`。

## 109 回程 table 2111（常见误配）

| 错误 | 正确 |
|------|------|
| `139.159.105.92/30` table 2111 **via 208** dev eno1np0 | **网关留空**，仅 `dev eno1np0`（OP 会自动加 `scope link`） |

文档要求回程 **勿** `via 208`；写 `via` 会把包先交给 208 作下一跳，MTR 回程/Reply 易超时。另需 **`ip rule` pref 29**（`apply_downstream_transit.py` 或等价配置）与 **`105.94` 静态邻居**（同 208 MAC），仅静态路由不够。
