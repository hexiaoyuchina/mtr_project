#!/usr/bin/env python3
"""109 只读：邻居启停 vs 通告、208 可达性、TX 残留 — 不改配置。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

DEPLOY_DIR = Path(__file__).resolve().parent


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY_DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        if name == "env":
            break


def main() -> int:
    load_env()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109")
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    if not pw:
        print("需要 109/env", file=sys.stderr)
        return 2

    script = r"""
set +e
export PEER=139.159.43.208
export VRF=vbgp13915943249
export SPOOF=139.159.43.249
echo "========== 时间 / 主机 =========="
date -Is; hostname

echo ""
echo "========== 1) 下游邻居 OP 状态 =========="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors?vrf=$VRF" | python3 -c "
import json,sys
for n in json.load(sys.stdin):
    if n.get('neighbor_ip')=='139.159.43.208':
        for k in ('neighbor_ip','source_ip','session_state','enabled','advertise_routes',
                  'store_received_routes','routes_received','routes_sent','routes_cached','role'):
            print(f'  {k}: {n.get(k)}')
"

echo ""
echo "========== 2) 通告任务（advertise / withdraw）=========="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors/$VRF/$PEER/advertise/status" 2>/dev/null
echo
curl -sf "http://127.0.0.1:9179/api/rib/advertise/status?task_id=$VRF-$PEER-advertise" 2>/dev/null
echo

echo ""
echo "========== 3) Agent TX 208 会话计数 =========="
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
d=json.load(sys.stdin)
for n in d.get('neighbors') or []:
    if n.get('address')=='139.159.43.208':
        print(json.dumps(n,indent=2,ensure_ascii=False))
"

echo ""
echo "========== 4) 208 连通（业务地址 43.208）=========="
echo -n "ping default: "; ping -c1 -W2 $PEER >/dev/null 2>&1 && echo OK || echo FAIL
echo -n "ping eno1np0: "; ping -c1 -W2 -I eno1np0 $PEER >/dev/null 2>&1 && echo OK || echo FAIL
echo -n "ping vrf+spoof: "; ip vrf exec $VRF ping -c1 -W2 -I $SPOOF $PEER >/dev/null 2>&1 && echo OK || echo FAIL
echo -n "tcp/8291 vrf: "; timeout 2 bash -c "echo >/dev/tcp/$PEER/8291" 2>/dev/null && echo OK || echo FAIL
echo -n "tcp/22 vrf: "; timeout 2 bash -c "echo >/dev/tcp/$PEER/22" 2>/dev/null && echo OK || echo FAIL
ss -tn state established | grep $PEER || echo "no_tcp_to_208"

echo ""
echo "========== 5) nft 249 DNAT（208 连 249:179）=========="
nft list table inet mtr_bgp_sat_dnat 2>/dev/null | grep -F "$SPOOF" || echo "MISSING dnat for $SPOOF"

echo ""
echo "========== 6) 最近 3h bgp-agent（208 / freeze / advertise）=========="
journalctl -u bgp-agent --since '3 hours ago' --no-pager 2>/dev/null | \
  grep -iE '208|freeze|unfreeze|208-advertise|AdminDown|passive connection' | tail -35

echo ""
echo "========== 7) SQLite meta（208 行）=========="
python3 <<'PY'
import sqlite3
try:
    c=sqlite3.connect('/root/mtr_op/data.db')
    c.row_factory=sqlite3.Row
    for r in c.execute("SELECT * FROM bgp_neighbor_meta WHERE neighbor_ip=? OR vrf=?", ('139.159.43.208','vbgp13915943249')):
        print(dict(r))
except Exception as e:
    print('meta err', e)
PY

echo ""
echo "========== 8) TX 本地是否仍有大量可再发路径（gobgp 若存在）=========="
for GB in /usr/local/bin/gobgp /usr/bin/gobgp; do [ -x "$GB" ] && break; done
if [ -x "$GB" ]; then
  PORT=$(python3 -c "
vrf='vbgp13915943249';h=0
for ch in vrf: h=(h*31+ord(ch))&0xFFFF
print(1790+1+(h%50))
")
  echo "tx_port=$PORT"
  "$GB" -p "$PORT" neighbor $PEER 2>/dev/null | head -12
  echo -n "adj-out count: "
  "$GB" -p "$PORT" neighbor $PEER adj-out 2>/dev/null | wc -l
else
  echo "gobgp CLI not in PATH (skip adj-out)"
fi

echo ""
echo "========== 9) 管理口 109（对比）=========="
ip -br addr show enp59s0f1np1 2>/dev/null
ip route show default
"""

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"SSH → {host}（只读）\n")
    try:
        c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    except Exception as e:
        print(f"SSH 失败: {e}")
        return 1
    try:
        i, o, e = c.exec_command("bash -se", timeout=120)
        i.write(script)
        i.channel.shutdown_write()
        print(o.read().decode(errors="replace"))
        err = e.read().decode(errors="replace")
        if err.strip():
            print("[stderr]", err[:500])
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
