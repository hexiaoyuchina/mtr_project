# 单元测试功能清单

更新时间：2026-05-22  
范围：生产 OP（`service/static/index.html` + `service/app/main.py`）  
说明：不含 VPN、不含原型/未接 API 功能；对齐 `docs/requirements-admin.md` 中已实现部分。

---

## 模块与页面

| 页面 | 模块 |
|------|------|
| 01 总览 | 全局开关、健康、KPI |
| 02 BGP 管理 | 邻居 / VRF / Agent |
| 03 BGP 路由 | 学习路由只读 |
| 04 逐跳替换 | MTR/ICMP 劫持规则 |
| 05 ARP 引流 | ARP + 卫星 VRF |
| 06 静态路由 | 主机 `ip route` |

---

## 1. 总览 / 全局（G）

| ID | 功能 | API / 逻辑 | 测试要点 |
|----|------|------------|----------|
| G-01 | 健康检查 | `GET /health` | 返回 200 |
| G-02 | MTR 劫持总开关 | `GET/PUT /api/global` | 读写一致；关/开时触发 nft/te_rewrite（可 mock） |
| G-03 | 总览统计 | global + hop + arp 列表 | 规则数、ARP 条数正确 |

**相关代码：** `service/app/main.py`，`service/app/storage.py`，`service/app/nft_sync.py`，`service/app/te_rewrite_sync.py`

---

## 2. 逐跳替换规则（HR）

| ID | 功能 | API | 测试要点 |
|----|------|-----|----------|
| HR-01 | 列表 | `GET /api/hop-rules` | 空库 / 多条 |
| HR-02 | 新增 | `POST /api/hop-rules` | 合法 CIDR；非法 422 |
| HR-03 | 编辑 | `PATCH /api/hop-rules/{id}` | 部分字段更新 |
| HR-04 | 删除 | `DELETE /api/hop-rules/{id}` | 不存在 404 |
| HR-05 | 规则 → nft | `nft_sync`（单测） | match / forge 字符串正确 |
| HR-06 | TE rewrite | `te_rewrite_sync`（单测或 mock） | 与 DB 规则一致 |

**相关代码：** `service/app/storage.py`（hop_replace），`service/app/hop_cidr.py`，`service/app/nft_sync.py`

---

## 3. ARP 引流（ARP）

| ID | 功能 | API | 测试要点 |
|----|------|-----|----------|
| ARP-01 | 总开关 | `GET/PUT /api/arp-spoof/settings` | 读写一致 |
| ARP-02 | 条目 CRUD | `GET/POST/PATCH/DELETE /api/arp-spoof/targets` | 冒充 IP 唯一 → 409 |
| ARP-03 | VRF 命名 | `vrf_naming`（单测） | IP → `vbgp*` 命名规则 |
| ARP-04 | 同步卫星 VRF | `POST /api/arp-spoof/satellite-vrfs/reconcile` | 幂等（mock 内核） |
| ARP-05 | ipvlan 收敛 | `POST /api/bgp/ipvlan-satellites/reconcile` | 幂等 |
| ARP-06 | 主机 /32 分配 | `arp_spoof_assign` | 开关联动（mock） |
| ARP-07 | 删除后 GARP 恢复 | `arp_neighbor_restore` | 纯逻辑 / mock |

**相关代码：** `service/app/bgp_ipvlan_reconcile.py`，`service/app/arp_spoof_assign.py`，`service/app/arp_neighbor_restore.py`，`service/app/vrf_naming.py`

---

## 4. BGP 管理（BGP）

| ID | 功能 | API | 测试要点 |
|----|------|-----|----------|
| BGP-01 | VRF 列表 | `GET /api/bgp/vrfs` | 结构完整 |
| BGP-02 | 建仓 | `POST /api/bgp/instances` | AS / Router-ID 校验 |
| BGP-03 | 邻居列表 | `GET /api/bgp/neighbors` | 角色、状态标签 |
| BGP-04 | 新增邻居 | `POST /api/bgp/neighbors` | mock Agent |
| BGP-05 | 编辑 / 删除 | `PATCH/DELETE /api/bgp/neighbors/{vrf}/{ip}` | meta 同步 |
| BGP-06 | 启停会话 | `POST .../toggle` | enabled 切换 |
| BGP-07 | 路由入库 | `POST .../store-routes` | 标志位持久化 |
| BGP-08 | 路由通告 | `POST .../advertise`，`GET .../advertise/status` | 异步任务状态 |
| BGP-09 | 从 FRR 同步 | `POST /api/bgp/sync-from-frr` | mock Agent |
| BGP-10 | 恢复 Agent | `POST /api/bgp/restore-agent` | mock reconcile |
| BGP-11 | 表单提示 | `GET /api/bgp/neighbor-form-hints` | RR / 卫星源 IP 列表 |
| BGP-12 | 卫星 VRF 只读 | `GET /api/bgp/satellite-vrfs` | 与 ARP 状态一致 |
| BGP-13 | 网卡列表 | `GET /api/host-ifaces` | 非空结构 |

**建议单测纯函数：**

- `bgp_control.is_rr_role` / `is_downstream_role`
- `storage.validate_ipv4` / `is_usable_bgp_source_ip` / `validate_vrf_name`
- `bgp_control.agent_row_to_state_label`

**相关代码：** `service/app/bgp_control.py`，`service/app/bgp_peer_rib.py`，`service/app/kernel_vrf.py`

---

## 5. BGP 学习路由（BR）

| ID | 功能 | API | 测试要点 |
|----|------|-----|----------|
| BR-01 | 过滤选项 | `GET /api/bgp/learned-routes/filter-options` | VRF / 邻居下拉 |
| BR-02 | 分页查询 | `GET /api/bgp/learned-routes` | 上游/下游窗、页码、空结果 |
| BR-03 | ingest | `POST /api/bgp/learned-routes/ingest` | mock RIB |
| BR-04 | sync | `POST /api/bgp/learned-routes/sync` | 计数 / 幂等 |
| BR-05 | AS_PATH 解析 | `first_asn_from_path`（单测） | 路径字符串 → 首 AS |
| BR-06 | 角色窗 | `peer_route_window`（单测） | RR vs 下游 |

**相关代码：** `service/app/bgp_learned_routes_sync.py`，`service/app/bgp_peer_rib.py`

---

## 6. 静态路由（SR）

| ID | 功能 | API | 测试要点 |
|----|------|-----|----------|
| SR-01 | 安装域列表 | `GET /api/static-routes/scopes` | main / table / vrf |
| SR-02 | CRUD | `GET/POST/PATCH/DELETE /api/static-routes` | 跨 VRF 校验；FIB 冲突 409 |
| SR-03 | 命令构建 | `build_route_argv` / `build_preview_cmds`（单测） | main/table/vrf/cross_vrf |
| SR-04 | 应用下发 | `POST /api/static-routes/apply` | dry_run、`applied` 状态文件 |
| SR-05 | 探测 | `POST /api/static-routes/probe` | mock `ip route get` |
| SR-06 | 内核对账 | `reconcile_one` | `applied` / `disabled` / `disabled_leak` |
| SR-07 | 停用撤回 | PATCH `enabled=false` / 全量 apply | 内核 `ip route del`；`withdrawn` 计数 |
| SR-08 | FIB 去重 | `find_static_route_by_fib_key` | 冲突 409 |

**环境变量：**

- `MTR_STATIC_ROUTE_DRY_RUN=1`：只打印命令
- `MTR_STATIC_ROUTE_AUTO_APPLY=1`：保存后自动下发（默认关）

**相关文档：** `docs/STATIC_ROUTES.md`  
**相关代码：** `service/app/static_route_sync.py`，`service/app/storage.py`（static_routes）

---

## 测试分层建议

| 优先级 | 层级 | 覆盖 |
|--------|------|------|
| P0 | 单元测试 | `storage` 校验、`static_route_sync` argv、`vrf_naming`、`hop_cidr`、`bgp_learned_routes_sync`、`bgp_ipvlan` 纯函数 |
| P1 | API 测试 | 上表全部 `/api/*`；设置 `MTR_OP_SKIP_NFT_SYNC=1`，mock `bgp_control` / Agent |
| P2 | 集成测试 | 可选：Linux 上 `static-routes` + `MTR_STATIC_ROUTE_DRY_RUN=1`；`109/verify.py` 场景 |

**推荐目录：** `service/tests/`（待建）

**环境约定（API 测试）：**

```bash
export MTR_OP_SKIP_NFT_SYNC=1
export MTR_STATIC_ROUTE_DRY_RUN=1
# 内存库或临时文件
export MTR_OP_DB=/tmp/mtr_op_test.db
```

---

## 用例模板

```python
# 静态路由 argv（单元）
def test_build_route_argv_main():
    route = make_static_route(install_scope="main", dst="10.0.0.0/8", gateway="192.0.2.1")
    argv = static_route_sync.build_route_argv(route)
    assert "replace" in argv
    assert "10.0.0.0/8" in argv


# hop-rule 非法 CIDR（API）
def test_hop_rule_bad_cidr(client):
    r = client.post("/api/hop-rules", json={"match_cidr": "not-a-cidr", "forge_ipv4": "1.1.1.1"})
    assert r.status_code == 422


# ARP 冒充 IP 重复（API）
def test_arp_duplicate_ip(client):
    client.post("/api/arp-spoof/targets", json={"spoof_gateway_ip": "1.2.3.4", "iface": "eth0"})
    r = client.post("/api/arp-spoof/targets", json={"spoof_gateway_ip": "1.2.3.4", "iface": "eth1"})
    assert r.status_code == 409
```

---

## 本期不纳入

| 类别 | 说明 |
|------|------|
| VPN | `/api/vpn/*` 与生产 UI「VPN 出口」页 |
| 原型未接 API | 路由缓存、IP 库、网关池、Transit 2110/2111、审计、RBAC、告警、组件健康大盘（见 `docs/admin-prototype.html`） |
| 无 REST 的数据表 | `storage` 中 gateway_reply 相关表 |
| 调试 API | `/api/gobgp/*`（后端已实现，生产 UI 未调用；如需可另建调试套件） |

---

## 统计

- **模块数：** 6  
- **用例 ID 数：** 约 40（G×3 + HR×6 + ARP×7 + BGP×13 + BR×6 + SR×6）  
- **参考需求：** `docs/requirements-admin.md`（已实现子集）  
- **生产 UI：** `service/static/index.html`  
- **OpenAPI：** 服务启动后 `/docs`
