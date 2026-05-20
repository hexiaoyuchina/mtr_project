#!/usr/bin/env python3
"""109 上联/下联抓包：外部公网 mtr 139.159.105.94 的流量走向。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

DEPLOY = Path(__file__).resolve().parent
for name in ("env", "env.example"):
    p = DEPLOY / name
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        break

UPLINK = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0")
DOWN = os.environ.get("MTR_OP_DOWNSTREAM_IFACE", "eno1np0")
SEC = int(os.environ.get("TCPDUMP_SEC", "30"))
TARGET_IP = "139.159.105.94"
FILTER = f"host {TARGET_IP}"

script = f"""set -e
UPLINK={UPLINK!r}
DOWN={DOWN!r}
FILTER={FILTER!r}
SEC={SEC}
TARGET={TARGET_IP}

echo "=== 【环境信息】 ==="
date -Is
hostname
echo "上联接口: $UPLINK"
echo "下联接口: $DOWN"
echo "抓包时长: ${SEC}s"
echo "过滤规则: $FILTER"
echo

echo "=== 【路由状态】 ==="
echo "--- 路由表 2110 (去程) ---"
ip route show table 2110 2>/dev/null || echo "table 2110 不存在"
echo "--- 路由表 2111 (回程) ---"
ip route show table 2111 2>/dev/null || echo "table 2111 不存在"
echo "--- 规则 29/30/31 ---"
ip -4 rule list | grep -E '^29:|^30:|^31:' || echo "无相关规则"
echo

echo "=== 【邻居状态】 ==="
ip neigh show dev "$DOWN" | grep -E "$TARGET|43.208" || echo "邻居表为空"
echo

echo "=== 【上联 $UPLINK 抓包】==="
echo "流量方向: 外网 -> 105.94 (去程) / 105.94 -> 外网 (回程)"
timeout "$SEC" tcpdump -ni "$UPLINK" -tttt -vv -c 100 "$FILTER" 2>&1 || true
echo

echo "=== 【下联 $DOWN 抓包】==="
echo "流量方向: 109 -> 105.94 (去程) / 105.94 -> 109 (回程)"
timeout "$SEC" tcpdump -ni "$DOWN" -tttt -vv -c 100 "$FILTER" 2>&1 || true
echo

echo "=== 【抓包结束】 ==="
echo "分析要点:"
echo "  1. 上联入: 源=外网, 目的=105.94 (去程)"
echo "  2. 下联出: 源=105.94, 目的=外网 (回程)"
echo "  3. 检查是否走管理口 enp59s0f1np1"
"""


def main() -> None:
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        print("请设置 MTR_OP_SSH_PASSWORD 环境变量或创建 109/env 文件", file=sys.stderr)
        sys.exit(2)
    
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109")
    user = os.environ.get("MTR_OP_SSH_USER", "root")
    
    print(f"=== 连接 109 ({host}) 开始抓包 ===")
    print(f"目标: {TARGET_IP}")
    print(f"上联: {UPLINK}, 下联: {DOWN}")
    print(f"时长: {SEC}秒\n")
    
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(host, username=user, password=pw, timeout=30)
        _, stdout, stderr = c.exec_command(f"bash -s <<'REMOTE_EOF'\n{script}\nREMOTE_EOF", timeout=SEC + 60)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        print(out)
        if err.strip():
            print("\n【错误输出】", err, file=sys.stderr)
    finally:
        c.close()


if __name__ == "__main__":
    main()
