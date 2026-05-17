# 文档索引

## BGP（现网最终架构）

| 文档 | 说明 |
|------|------|
| **[BGP_ARCHITECTURE.md](./BGP_ARCHITECTURE.md)** | **首选**：双向学/存/冻/搬、RX/TX、SQLite 与 Agent 分工 |
| **[BGP_DATA_AND_API.md](./BGP_DATA_AND_API.md)** | SQLite 表字段、OP `:8808` 与 Agent `:9179` 接口 |
| **[BGP_OP_NETWORK.md](./BGP_OP_NETWORK.md)** | 三网口分工（207 RR / 208 下游 / 109 管理） |
| **[BGP_RXTX_DEPLOYMENT.md](./BGP_RXTX_DEPLOYMENT.md)** | 编译、systemd、环境变量、验收清单 |
| [BGP_ARP_SPOOF_MULTI_SESSION.md](./BGP_ARP_SPOOF_MULTI_SESSION.md) | ARP 代答 + 多 VRF 冒充（内核路由侧） |
| [bgp-ipvlan-setup.md](./bgp-ipvlan-setup.md) | 卫星 ipvlan（**实验室** `10.133.152.*`） |

## 运维与其它

| 文档 | 说明 |
|------|------|
| **[部署.md](./部署.md)** | 日常部署：上传代码 + 重启服务 |
| [VPN_EGRESS_DESIGN_NOTES.md](./VPN_EGRESS_DESIGN_NOTES.md) | VPN 出口设计 |
| [VPN_EGRESS_OPS.md](./VPN_EGRESS_OPS.md) | VPN 出口运维 |
| [requirements-admin.md](./requirements-admin.md) | 管理后台需求 |
| [admin-prototype.html](./admin-prototype.html) | UI 原型（不接线 API） |

根目录历史记录：[story.md](../story.md)、[step.md](../step.md)。
