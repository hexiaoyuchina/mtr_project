#!/usr/bin/env python3
"""检查 153.200 / 153.204 / 152.204 是否收到 BGP 路由（控制面 + Agent）。"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
TARGETS = (
    "10.133.153.20",  # 用户原文，可能笔误
    "10.133.153.200",
    "10.133.153.204",
    "10.133.152.204",
)


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def get_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def run_ssh(ssh: paramiko.SSHClient, cmd: str) -> str:
    _, stdout, stderr = ssh.exec_command(cmd, timeout=90)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    return out + (f"\n[stderr]\n{err}" if err.strip() else "")


def main() -> int:
    load_lab_env()
    host = os.environ["MTR_OP_HOST"]
    op = f"http://{host}:{os.environ.get('MTR_OP_PORT', '8808')}"
    agent = f"http://{host}:9179"

    print(f"控制面 {host}\n")

    nb = get_json(f"{op}/api/bgp/neighbors")
    if isinstance(nb, list):
        print("=== OP 邻居（153/152 相关）===")
        for n in nb:
            nip = str(n.get("neighbor_ip") or "")
            if any(t in nip or nip.startswith("10.133.153.2") for t in TARGETS):
                print(
                    json.dumps(
                        {
                            "vrf": n.get("vrf"),
                            "neighbor_ip": nip,
                            "role": n.get("role"),
                            "source_ip": n.get("source_ip"),
                            "advertise_routes": n.get("advertise_routes"),
                            "routes_received": n.get("routes_received"),
                            "routes_sent": n.get("routes_sent"),
                        },
                        ensure_ascii=False,
                    )
                )
        for nip in ("10.133.153.204", "10.133.152.204"):
            row = next((x for x in nb if str(x.get("neighbor_ip")) == nip), None)
            if row:
                vrf = str(row["vrf"])
                st = get_json(
                    f"{op}/api/bgp/neighbors/{urllib.parse.quote(vrf)}/{nip}/advertise/status"
                )
                print(f"\nadvertise/status {vrf}/{nip}:")
                print(json.dumps(st, ensure_ascii=False))

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        host,
        username=os.environ["MTR_OP_SSH_USER"],
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=20,
    )
    try:
        py = r"""python3 <<'PY'
import json, urllib.request
def g(u):
    with urllib.request.urlopen(u, timeout=30) as r:
        return json.load(r)
nb = g('http://127.0.0.1:9179/api/neighbors')
if isinstance(nb, dict):
    nb = nb.get('neighbors') or []
print('=== Agent 会话（本机视角：对端是否 Established，本机发出 pfx_adv）===')
for n in nb:
    a = str(n.get('address') or '')
    if a in ('10.133.153.204', '10.133.152.204'):
        print(json.dumps({
            'vrf': n.get('vrf'), 'peer': a, 'state': n.get('state'),
            'session': n.get('session'), 'local_address': n.get('local_address'),
            'pfx_rcd': n.get('pfx_rcd'), 'pfx_adv': n.get('pfx_adv'),
        }, ensure_ascii=False))
PY"""
        print(run_ssh(ssh, py))

        print("=== gobgp adj-out 条数（本机发给对端的前缀数）===")
        print(
            run_ssh(
                ssh,
                "for p in 50051 50052; do "
                "echo port=$p; "
                "gobgp -p $p neighbor 2>/dev/null | awk '/^[0-9]/{{print}}' | head -20; "
                "gobgp -p $p neighbor 10.133.153.204 adj-out 2>/dev/null | wc -l; "
                "gobgp -p $p neighbor 10.133.152.204 adj-out 2>/dev/null | wc -l; "
                "done",
            )
        )

        print("=== 从 153.200 ping 153.204（链路）===")
        print(run_ssh(ssh, "ping -c2 -W2 -I 10.133.153.200 10.133.153.204 2>&1 | tail -4"))

        print("=== 探测 10.133.153.20 是否存在 ===")
        print(
            run_ssh(
                ssh,
                "ping -c1 -W1 10.133.153.20 2>&1; "
                "ip route get 10.133.153.20 2>&1 | head -2",
            )
        )
    finally:
        ssh.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
