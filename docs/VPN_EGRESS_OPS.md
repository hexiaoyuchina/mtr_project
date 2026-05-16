# VPN 出口 — 运维说明

> 正式运维界面：**[`service/static/index.html`](../service/static/index.html)**（由 `GET /` 挂载，同源调用 `/api/vpn/*`）。[`docs/admin-prototype.html`](./admin-prototype.html) **仅为产品原型**，不作为线上前端。

---

## 1. 常用 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/vpn/summary` | 隧道数量统计（在线/未就绪/禁用） |
| GET | `/api/vpn/links` | 隧道列表 |
| POST | `/api/vpn/links` | 新增隧道 |
| PATCH | `/api/vpn/links/{id}` | 修改启用、期望 up、配置等 |
| DELETE | `/api/vpn/links/{id}` | 删除（若被策略引用则失败） |
| GET | `/api/vpn/policies` | 策略列表 |
| POST | `/api/vpn/policies` | 新增策略 |
| POST | `/api/vpn/apply` | **幂等下发**（GRE/OpenVPN/L2TP 配置包 + 策略 `ip rule`） |
| POST | `/api/vpn/ping` | 在指定 VRF 内 ping |
| GET | `/api/vpn/events` | 最近事件日志 |

---

## 2. 环境变量（摘要）

| 变量 | 作用 |
|------|------|
| `MTR_OP_VPN_APPLY` | `0`：不下发内核（非 Linux 调试）；默认在 Linux 下发 |
| `MTR_OP_DATA` | 数据目录父路径；VPN 子目录为 `vpn/`、`vpn/l2tp-<id>/` |
| `MTR_VPN_POLICY_TABLE_BASE` | 策略独立路由表起始编号（默认 `33700`） |
| `MTR_VPN_POLICY_RULE_PREF_BASE` | `ip rule pref` 起始（默认 `28000`） |
| `MTR_VPN_RECONCILE` | `0`：关闭后台状态/计数刷新 |
| `MTR_L2TP_APPLY` | `1`：生成 L2TP 包后 **best-effort** 执行 `ipsec stroke` / `xl2tpd` reload（可能因发行版差异失败） |

---

## 3. 排障

1. **下发无效果**：确认本机为 Linux 且 `MTR_OP_VPN_APPLY` 未禁用；服务用户是否有权执行 `ip`/`openvpn`（常见为 root）。  
2. **策略不命中**：`ip rule list`、`ip route show table <33700+策略id>`；与 FRR/静态表冲突时调高 `MTR_VPN_POLICY_RULE_PREF_BASE` 或改 table 基数（见开发计划）。  
3. **OpenVPN 起不来**：看 `last_error`；检查 `data/vpn/openvpn-<id>.conf` 与证书路径；`journalctl` / OpenVPN 日志。  
4. **L2TP**：默认只生成 **`data/vpn/l2tp-<id>/`** 下片段，需按包内 `README.txt` 合并到系统 **xl2tpd / ipsec** 后再拨号；`last_error` 形如 `l2tp_bundle_ready:...` 表示包已就绪。  
5. **VRF 未挂上**：GRE/OpenVPN 由代码 `master vrf2103`；L2TP 依赖 `ip-up-vrf.sh` 在 PPP up 时执行。  
6. **跨 VRF 回程 / 反向路径过滤**：OP **不会**自动改 `rp_filter` / `ip_forward`；与现网 **`step.md`** 基线 sysctl 对齐，变更 VPN 后若 ICMP/TCP 异常优先核对接口 sysctl 与策略路由。

---

## 4. 关联文档

- [VPN_EGRESS_DEVELOPMENT_PLAN.md](./VPN_EGRESS_DEVELOPMENT_PLAN.md)  
- **[VPN_EGRESS_DESIGN_NOTES.md](./VPN_EGRESS_DESIGN_NOTES.md)**（表结构、API、内核编号、FRR 边界）  
- [VPN_EGRESS_DESIGN_NOTES.md](./VPN_EGRESS_DESIGN_NOTES.md)  
- [VPN_EGRESS_IMPLEMENTATION_CHECKLIST.md](./VPN_EGRESS_IMPLEMENTATION_CHECKLIST.md)
