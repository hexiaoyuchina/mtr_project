#!/usr/bin/env python3
"""SSH 到实验室 200，查看 152.204 / 153.204 的 gobgp 会话与通告计数。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 60) -> str:
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    if err.strip():
        out += "\n[stderr]\n" + err
    return out


def main() -> int:
    load_lab_env()
    host = os.environ["MTR_OP_HOST"]
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
for n in nb:
    a = str(n.get('address') or '')
    if a in ('10.133.152.204', '10.133.153.204'):
        print(json.dumps({
            'vrf': n.get('vrf'), 'address': a, 'state': n.get('state'),
            'enabled': n.get('enabled'), 'pfx_rcd': n.get('pfx_rcd'),
            'pfx_adv': n.get('pfx_adv'), 'session': n.get('session'),
            'local_address': n.get('local_address'),
        }, ensure_ascii=False))
PY"""
        print("=== Agent /api/neighbors ===")
        print(run(ssh, py))

        print("=== gobgp vbgp10133153204 peer 152.204 ===")
        print(run(ssh, "gobgp -p 50051 neighbor 10.133.152.204 2>/dev/null | head -30"))

        print("=== gobgp gobgp-rr peer 153.204 ===")
        print(run(ssh, "gobgp -p 50052 neighbor 10.133.153.204 2>/dev/null | head -30"))

        print("=== sqlite advertise flag ===")
        print(
            run(
                ssh,
                "test -f /root/mtr_op/data.db && sqlite3 /root/mtr_op/data.db "
                "\"SELECT vrf,neighbor_ip,source_ip,advertise_routes,ingest_routes "
                "FROM bgp_neighbor_meta WHERE neighbor_ip IN "
                "('10.133.152.204','10.133.153.204') ORDER BY vrf;\" "
                "|| echo no_db",
            )
        )

        print("=== OP log advertise (last 15) ===")
        print(
            run(
                ssh,
                "grep -i advertise /tmp/mtr_op.log 2>/dev/null | tail -15 || echo no_log",
            )
        )
    finally:
        ssh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
