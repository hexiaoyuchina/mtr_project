#!/usr/bin/env python3
"""对比 RR(153.204/151.210) 与 服务(153.200/151.200) 的 BGP 路由数量。"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
PW = "1234qwer"
H200 = "10.133.151.200"
H210 = "10.133.151.210"
RR_BGP = "10.133.153.204"
SVC_BGP = "10.133.153.200"


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def ssh(host: str, user: str, script: str, password: str | None = None, timeout: int = 90) -> tuple[int, str]:
    pw = password or PW
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=user, password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    try:
        i, o, e = c.exec_command("bash -se" if user == "root" else script, timeout=timeout)
        if user == "root":
            i.write(script)
            i.channel.shutdown_write()
            out = o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")
            return o.channel.recv_exit_status(), out
        out = o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")
        return o.channel.recv_exit_status(), out
    finally:
        c.close()


def ros_cmd(cmd: str, password: str) -> tuple[int, str]:
    """RouterOS: 单条命令经 SSH 非交互执行。"""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H210, username="admin", password=password, timeout=25, allow_agent=False, look_for_keys=False)
    try:
        # MikroTik 接受 ssh admin@host "/command"
        full = cmd if cmd.startswith("/") else f"/{cmd}"
        _, o, e = c.exec_command(full, timeout=60)
        out = o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")
        return o.channel.recv_exit_status(), out
    finally:
        c.close()


def main() -> int:
    load_lab_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", PW)

    print("=" * 60)
    print(f"服务侧 Linux 200 管理 {H200}  (BGP 源 {SVC_BGP} vrf2103)")
    print("=" * 60)

    code, out = ssh(
        H200,
        "root",
        f"""
set -e
echo '--- ens224 / vrf2103 ---'
ip -br addr show ens224 2>/dev/null || true
ip addr show vrf vrf2103 2>/dev/null | head -8 || true
echo '--- BGP TCP to RR ---'
ss -tnp state established 2>/dev/null | grep -E '{RR_BGP}|{SVC_BGP}|:179' || true
echo '--- Agent status ---'
curl -sf http://127.0.0.1:9179/health && echo
curl -s http://127.0.0.1:9179/api/status | python3 -m json.tool 2>/dev/null || curl -s http://127.0.0.1:9179/api/status
echo
echo '--- freeze-status (upstream RR) ---'
curl -s http://127.0.0.1:9179/api/peers/freeze-status | python3 -m json.tool 2>/dev/null || curl -s http://127.0.0.1:9179/api/peers/freeze-status
echo
echo '--- Agent neighbors (gobgp-rr / 153.204) ---'
curl -s http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
d=json.load(sys.stdin)
for n in d.get('neighbors',[]):
    if n.get('vrf')=='gobgp-rr' or '{RR_BGP}' in str(n.get('neighbor_ip','')):
        print(json.dumps(n, indent=2, ensure_ascii=False))
"
echo '--- OP neighbor row ---'
curl -s http://127.0.0.1:8808/api/bgp/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin):
    if n.get('vrf')=='gobgp-rr':
        print(json.dumps({{k:n.get(k) for k in ('vrf','neighbor_ip','session_state','routes_received','routes_sent','pfx_rcd','pfx_adv','advertise_routes')}}, ensure_ascii=False))
"
echo '--- SQLite learned (upstream / gobgp-rr) ---'
DB=/root/mtr_op/data.db
if [ -f "$DB" ]; then
  sqlite3 "$DB" "SELECT route_window, COUNT(*) FROM bgp_learned_routes WHERE vrf='gobgp-rr' GROUP BY route_window;"
  sqlite3 "$DB" "SELECT COUNT(*) AS total_upstream FROM bgp_learned_routes WHERE vrf='gobgp-rr' AND route_window='upstream';"
  sqlite3 "$DB" "SELECT prefix,nexthop FROM bgp_learned_routes WHERE vrf='gobgp-rr' AND route_window='upstream' LIMIT 5;"
else
  echo "no data.db"
fi
echo '--- Redis/rocksdb route hints (if any) ---'
curl -s http://127.0.0.1:9179/api/status 2>/dev/null | grep -o route_count || true
""",
        password=pw,
    )
    print(out)
    if code != 0:
        print(f"[WARN] 200 script exit={code}")

    print("\n" + "=" * 60)
    print(f"RR 侧 RouterOS 210 管理 {H210}  (BGP {RR_BGP})")
    print("=" * 60)

    for cmd in (
        "/routing bgp connection print detail where remote.address=" + SVC_BGP,
        "/routing bgp peer print detail",
        "/routing bgp advertisement print count-only where peer~" + SVC_BGP.replace(".", "\\."),
        "/routing route print count-only where protocol=bgp",
        "/routing route print count-only where bgp and dst-address!~\"0.0.0.0/0\"",
    ):
        print(f"\n>>> {cmd}")
        try:
            rc, rout = ros_cmd(cmd, pw)
            print(rout[:8000] if rout else "(empty)")
            if rc != 0:
                print(f"[exit {rc}]")
        except Exception as ex:
            print(f"ERR: {ex}")

    # 备用：旧版 peer 语法
    for cmd in (
        "/routing bgp peer print detail where remote-address=" + SVC_BGP,
        "/routing bgp network print",
    ):
        print(f"\n>>> {cmd}")
        try:
            _, rout = ros_cmd(cmd, pw)
            print(rout[:4000] if rout else "(empty)")
        except Exception as ex:
            print(f"ERR: {ex}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
