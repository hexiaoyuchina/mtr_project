#!/usr/bin/env python3
"""109 MTR 不通：转发面 + OP 静态路由 + NFQUEUE 一键诊断。"""
from __future__ import annotations

import os
from pathlib import Path

import paramiko

DIR = Path(__file__).resolve().parent
SRC = "139.159.105.94"
DST = "8.8.8.8"
PEER = "139.159.43.208"
DOWN = "eno1np0"
UP = "enp59s0f0np0"


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
        break


def main() -> None:
    load_env()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ["MTR_OP_HOST"].strip(),
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    script = f"""
echo "========== 时间 / 转发 =========="
date
sysctl -n net.ipv4.ip_forward

echo "========== eno1np0 地址（勿含 {SRC}/32）=========="
ip -4 addr show dev {DOWN} | grep inet

echo "========== ip rule 29/30 =========="
ip -4 rule list | grep -E '^29:|^30:|^31:' || true

echo "========== table 2110 =========="
ip route show table 2110

echo "========== table 2111 =========="
ip route show table 2111

echo "========== main 上 105/208 冲突 =========="
ip route show table main | grep -E '105\\.94|43\\.208|default' | head -15

echo "========== 路径探测 =========="
echo -n "fwd: "; ip route get {DST} from {SRC} iif {DOWN} 2>&1 | head -1
echo -n "ret: "; ip route get {SRC} from {DST} iif {UP} 2>&1 | head -1
echo -n "local105: "; ip route get {SRC} 2>&1 | head -1

echo "========== 邻居 =========="
ip neigh show dev {DOWN} | grep -E '105\\.94|43\\.208' || true

echo "========== OP static_routes =========="
/root/mtr_op/venv/bin/python3 <<'PY'
import sqlite3, json
c = sqlite3.connect("/root/mtr_op/data.db")
rows = list(c.execute(
    "SELECT id,enabled,dst_cidr,gateway_ip,egress_iface,install_scope,table_id,note "
    "FROM static_routes ORDER BY id"
))
for r in rows:
    print(r)
print("hijack", c.execute("SELECT hijack_enabled FROM global_config").fetchone())
print("hop_rules", c.execute("SELECT COUNT(*) FROM hop_replace_rules WHERE enabled=1").fetchone())
PY

echo "========== reconcile API sample =========="
curl -sS 'http://127.0.0.1:8808/api/static-routes?reconcile=1' | /root/mtr_op/venv/bin/python3 -c "
import sys,json
for r in json.load(sys.stdin):
    print(r.get('id'), r.get('enabled'), r.get('dst_cidr'), r.get('sync_state'), r.get('kernel_line'))
"

echo "========== hijack / te =========="
cat /tmp/mtr_te_map.env 2>/dev/null || true
tail -2 /tmp/te_rewrite_nfqueue.log 2>/dev/null
pgrep -af te_rewrite || true

echo "========== ping =========="
ping -c1 -W1 -I {DOWN} {SRC} 2>&1 | tail -2
ping -c1 -W1 {PEER} 2>&1 | tail -2

echo "========== 5s 下联 ICMP 105.94/8.8.8.8 =========="
timeout 5 tcpdump -ni {DOWN} -c 15 'icmp and (host {SRC} or host {DST})' 2>&1 | head -20 || true
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=60)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
