#!/usr/bin/env python3
"""RR 聚合通告 added=1 后，153.204 侧是否收到：查 gobgp RX adj-out / global / Agent。"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
RR = "10.133.153.204"


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def get_json(url: str, timeout: int = 60) -> object:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def ssh_run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    return stdout.read().decode("utf-8", "replace") + stderr.read().decode("utf-8", "replace")


def main() -> int:
    load_lab_env()
    host = os.environ["MTR_OP_HOST"]
    op = f"http://{host}:8808"
    agent = f"http://{host}:9179"

    print("=== 1) OP 通告任务状态 ===")
    st = get_json(
        f"{op}/api/bgp/neighbors/gobgp-rr/{RR}/advertise/status"
    )
    print(json.dumps(st, ensure_ascii=False, indent=2))

    print("\n=== 2) Agent RR 邻居 pfx_rcd / pfx_adv ===")
    nb = get_json(f"{agent}/api/neighbors")
    if isinstance(nb, dict):
        nb = nb.get("neighbors") or []
    for n in nb:
        if str(n.get("address")) == RR:
            print(json.dumps(n, ensure_ascii=False, indent=2))

    tid = f"gobgp-rr-{RR}-advertise"
    try:
        jst = get_json(
            f"{agent}/api/rib/advertise/status?{urllib.parse.urlencode({'task_id': tid})}"
        )
        print("\n=== 3) Agent rib job ===")
        print(json.dumps(jst, ensure_ascii=False, indent=2))
    except Exception as e:
        print("rib job status:", e)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        host,
        username=os.environ["MTR_OP_SSH_USER"],
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=20,
    )
    try:
        script = f"""
set +e
RR={RR}
echo "=== 4) gobgp RX :50052 neighbor state ==="
gobgp -p 50052 neighbor $RR 2>/dev/null | head -20

echo "=== 5) adj-out 条数与前 5 条 ==="
n=$(gobgp -p 50052 neighbor $RR adj-out 2>/dev/null | wc -l)
echo "adj-out lines: $n"
gobgp -p 50052 neighbor $RR adj-out 2>/dev/null | head -8

echo "=== 5b) global rib 203.0.113.201/32 ==="
gobgp -p 50052 global rib -a ipv4 203.0.113.201/32 2>&1 | head -12

echo "=== 6) global rib 本机注入前缀（非 RR 学到的）==="
gobgp -p 50052 global rib -a ipv4 2>/dev/null | grep -E '0\\.0\\.0\\.0|10\\.|172\\.|192\\.' | head -15

echo "=== 7) 下游库唯一 1 条路由内容 ==="
python3 <<'PY'
import json, urllib.parse, urllib.request
ag = "http://127.0.0.1:9179"
q = urllib.parse.urlencode({{"window":"downstream","vrf":"vbgp10133153204","neighbor_ip":"10.133.152.204","page":"1","page_size":"5"}})
# try list endpoint
for path in (
    "/api/rib/routes?" + q,
    "/api/tx/learned-routes?vrf=vbgp10133153204&page=1&page_size=5",
):
    try:
        with urllib.request.urlopen(ag + path, timeout=30) as r:
            j = json.load(r)
        print("PATH", path[:60])
        if isinstance(j, dict):
            items = j.get("routes") or j.get("items") or j.get("data") or []
            print(json.dumps(items[:3], ensure_ascii=False, indent=2))
        elif isinstance(j, list):
            print(json.dumps(j[:3], ensure_ascii=False, indent=2))
    except Exception as e:
        print("PATH fail", path[:50], e)
PY

echo "=== 8) 从 153.200 看 BGP TCP 179 到 153.204（仅连接）==="
ss -tn state established '( sport = :179 or dport = :179 )' 2>/dev/null | grep 153.153 || ss -tn | grep "$RR.*179\\|179.*$RR" | head -5
"""
        print(ssh_run(ssh, script))

        # 若 lab 能 SSH 到 153.204（ROS），尝试 show bgp
        ros_pw = os.environ.get("MTR_ROS_SSH_PASSWORD", "")
        if ros_pw:
            print("=== 9) ROS 153.204 show bgp（若配置密码）===")
        else:
            print("\n=== 9) 未配置 MTR_ROS_SSH_PASSWORD，跳过 ROS 侧验证 ===")
            print("（本机 adj-out 为权威：>0 表示已发出 UPDATE；=0 表示 RR 未收到）")
    finally:
        ssh.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
