# Linux 200：BGP 验收（GoBGP Agent）

200 上 **已停用 FRR**（`remote-restart.sh` 会 `systemctl stop frr`）。BGP 会话在 **bgp-agent**，OP 在 **8808**。

## 会话与邻居

```bash
# Agent 原始邻居列表（推荐）
curl -s http://127.0.0.1:9179/api/neighbors | python3 -m json.tool

# 冻链 / Established（按上游窗、下游窗）
curl -s http://127.0.0.1:9179/api/peers/freeze-status | python3 -m json.tool

# OP 合并 meta 后的列表（与 Web「BGP 管理」一致）
curl -s http://127.0.0.1:8808/api/bgp/neighbors | python3 -m json.tool
```

## 卫星 VRF 示例（10.133.152.235）

```bash
VRF=vbgp10133152235
curl -s http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
    if n.get('vrf')=='$VRF': print(n)
"
ip route show vrf $VRF
ss -tnp | grep 152.235
```

## 不要用

- `vtysh -c 'show bgp summary'` — 200 无 FRR BGP 会话
- `POST /api/bgp/sync-from-frr` 名称含 frr，实际是从 **agent** 同步 meta，不是调 vtysh

## 对端 Linux 201

201 需配置接受来自冒充 IP 的 BGP（如 `neighbor 10.133.152.235 remote-as 63199`）。  
201 用什么软件（FRR 或其它）由 201 环境决定；**200 侧只认 Agent 状态**。
