#!/usr/bin/env python3
"""200 环境：部署后恢复网络前提与 BGP 邻居。"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    raise SystemExit(2)

LAB_DIR = Path(__file__).resolve().parent


def load_lab_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (LAB_DIR / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def main() -> None:
    env = load_lab_env()
    host = env["MTR_OP_HOST"]
    pw = env["MTR_OP_SSH_PASSWORD"]
    remote = env.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")

    script = f"""
set -e
bash {remote}/remote-network-prereq.sh
python3 <<'PY'
import json, urllib.request, time
AS=63199
agent='http://127.0.0.1:9179'
base='http://127.0.0.1:8808'
def req(m,u,b=None):
 d=json.dumps(b).encode() if b else None
 r=urllib.request.Request(u,data=d,method=m,headers={{'Content-Type':'application/json'}} if d else {{}})
 return urllib.request.urlopen(r,timeout=60).read().decode()
print('rr', req('POST', agent+'/api/rr/config', {{'address':'10.133.153.204','remote_as':AS,'local_address':'10.133.153.200'}}))
print('sync', req('POST', base+'/api/bgp/sync-from-frr'))
print('down', req('PATCH', base+'/api/bgp/neighbors/default/10.133.152.204', {{'remote_as':AS,'local_as':AS,'source_ip':'10.133.152.200','role':'downstream'}}))
time.sleep(5)
print('freeze', req('GET', agent+'/api/peers/freeze-status'))
print('ss', __import__('subprocess').check_output(['ss','-tnp','state','established'],text=True))
PY
"""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    i, o, e = c.exec_command("bash -se", timeout=90)
    i.write(script)
    i.channel.shutdown_write()
    print(o.read().decode())
    err = e.read().decode()
    if err:
        print(err, file=sys.stderr)
    c.close()


if __name__ == "__main__":
    main()
