#!/usr/bin/env python3
"""
在 RouterOS RR 上添加 100 条静态路由并经 BGP 通告给 Linux 200，验收 Agent/OP/SQLite。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H200 = "10.133.151.200"
H210 = "10.133.151.210"
RR_IP = "10.133.153.204"
SVC_IP = "10.133.153.200"
OP_PORT = "8808"
AGENT_PORT = "9179"
COMMENT = "lab-sim-bgp-200"
BASE_NET = "100.64"  # 100.64.1.0/24 .. 100.64.100.0/24


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def ros_exec(c: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    _, o, e = c.exec_command(cmd, timeout=timeout)
    return (o.read() + e.read()).decode("utf-8", "replace")


def http_json(url: str, method: str = "GET", body: dict | None = None, timeout: int = 30):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}


def ssh200_script(pw: str, script: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H200, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    try:
        i, o, e = c.exec_command("bash -se", timeout=120)
        i.write(script)
        i.channel.shutdown_write()
        return (o.read() + e.read()).decode("utf-8", "replace")
    finally:
        c.close()


def build_rsc() -> str:
    lines = [
        "# lab sim 100 static -> BGP -> linux 200",
        f"/routing bgp peer set [find name=peer-lin200-153] default-originate=never",
        f"/ip route remove [find comment={COMMENT}]",
        f"/routing bgp network remove [find comment={COMMENT}]",
    ]
    for i in range(1, 101):
        pfx = f"{BASE_NET}.{i}.0/24"
        lines.append(
            f"/ip route add dst-address={pfx} gateway=10.133.151.254 distance=1 comment={COMMENT}"
        )
        lines.append(
            f"/routing bgp network add network={pfx} synchronize=no comment={COMMENT}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    load_lab_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")
    op_base = f"http://{os.environ.get('MTR_OP_HOST', H200)}:{OP_PORT}"

    print("=== 1. RouterOS：导入 100 条静态 + bgp network ===\n")
    rsc = build_rsc()
    rsc_path = "/lab-sim-100.rsc"
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H210, username="admin", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    try:
        sftp = c.open_sftp()
        with sftp.file(rsc_path, "w") as f:
            f.write(rsc)
        sftp.close()
        print(ros_exec(c, f"/import file-name={rsc_path}"))
        time.sleep(3)
        print("--- bgp network count (lab) ---")
        print(ros_exec(c, f'/routing bgp network print count-only where comment="{COMMENT}"'))
        print("--- advertisements to 200 ---")
        print(ros_exec(c, "/routing bgp advertisements print")[:8000])
        print("--- peer status ---")
        print(ros_exec(c, '/routing bgp peer print status where name="peer-lin200-153"'))
    finally:
        c.close()

    print("\n=== 2. 等待 BGP 收敛 (15s) ===")
    time.sleep(15)

    print("\n=== 3. Linux 200：Agent / OP ===\n")
    agent_base = f"http://{H200}:{AGENT_PORT}"
    try:
        neigh = http_json(f"{agent_base}/api/neighbors")
        for n in neigh.get("neighbors", []):
            if n.get("vrf") == "gobgp-rr" or n.get("address") == RR_IP:
                print("Agent neighbor:", json.dumps(n, ensure_ascii=False, indent=2))
        rc = http_json(f"{agent_base}/api/routes/count")
        print("Agent /api/routes/count:", rc)
    except Exception as ex:
        print("Agent API err:", ex)

    try:
        rows = http_json(f"{op_base}/api/bgp/neighbors")
        for n in rows:
            if n.get("vrf") == "gobgp-rr":
                print("OP neighbor:", json.dumps(
                    {k: n.get(k) for k in (
                        "vrf", "neighbor_ip", "session_state", "routes_received",
                        "routes_sent", "advertise_routes",
                    )},
                    ensure_ascii=False,
                ))
    except Exception as ex:
        print("OP API err:", ex)

    print("\n=== 4. 触发 RIB 同步并查 SQLite ===\n")
    try:
        sync = http_json(f"{op_base}/api/bgp/learned-routes/sync", method="POST")
        print("sync-now:", json.dumps(sync, ensure_ascii=False)[:500])
    except Exception as ex:
        print("sync-now err:", ex)

    time.sleep(2)
    out = ssh200_script(
        pw,
        f"""
/root/mtr_op/venv/bin/python3 - <<'PY'
import sqlite3, json, urllib.request
db='/root/mtr_op/data.db'
c=sqlite3.connect(db)
n=c.execute("SELECT COUNT(*) FROM bgp_learned_routes WHERE vrf='gobgp-rr' AND neighbor_ip=? AND route_window='upstream'", ('{RR_IP}',)).fetchone()[0]
print('sqlite_upstream_count', n)
rows=c.execute("SELECT prefix FROM bgp_learned_routes WHERE vrf='gobgp-rr' AND neighbor_ip=? ORDER BY prefix LIMIT 5", ('{RR_IP}',)).fetchall()
print('sample', [r[0] for r in rows])
rows2=c.execute("SELECT prefix FROM bgp_learned_routes WHERE vrf='gobgp-rr' AND neighbor_ip=? ORDER BY prefix DESC LIMIT 3", ('{RR_IP}',)).fetchall()
print('sample_tail', [r[0] for r in rows2])
c.close()
try:
    d=json.load(urllib.request.urlopen('http://127.0.0.1:9179/api/routes/count'))
    print('agent_routes_count_api', d)
except Exception as e:
    print('agent_routes_count_err', e)
PY
""",
    )
    print(out)

    # Summary
    try:
        rows = http_json(f"{op_base}/api/bgp/neighbors")
        rr = next((x for x in rows if x.get("vrf") == "gobgp-rr"), {})
        rx = int(rr.get("routes_received") or 0)
    except Exception:
        rx = -1

    ok_ui = rx >= 100
    print("\n=== 验收 ===")
    print(f"OP routes_received (界面「收到」): {rx}  {'OK' if ok_ui else 'EXPECT>=100'}")
  # parse sqlite from output
    sqlite_n = 0
    for line in out.splitlines():
        if line.startswith("sqlite_upstream_count"):
            sqlite_n = int(line.split()[-1])
    print(f"SQLite upstream 缓存: {sqlite_n}  {'OK' if sqlite_n >= 100 else 'EXPECT>=100'}")
    return 0 if ok_ui and sqlite_n >= 100 else 1


if __name__ == "__main__":
    raise SystemExit(main())
