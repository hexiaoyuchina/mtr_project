#!/usr/bin/env python3
"""Linux 200：拉起 ens192/ens224，跑 network-prereq，验证 ping 与 BGP。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H200, H201 = "10.133.151.200", "10.133.151.201"
VRF = "vbgp10133153204"
PEER = "10.133.152.204"
SPOOF = "10.133.153.204"
LOCAL_RR = "10.133.153.200"
RR = "10.133.153.204"


def load_env() -> str:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def run(host: str, pw: str, script: str, timeout: int = 180) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=45, allow_agent=False, look_for_keys=False, banner_timeout=45)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    pw = load_env()
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")

    print("=== 200: link up + prereq ===\n")
    print(
        run(
            H200,
            pw,
            f"""
set -e
ip link set ens192 up
ip link set ens224 up
sleep 1
bash {remote}/remote-network-prereq.sh
ip -br link show ens192 ens224 iv204 2>/dev/null || true
ping -c2 -W2 {PEER}
""",
        )
    )

    print("\n=== 200: bgp-agent 状态与监听端口 ===\n")
    print(
        run(
            H200,
            pw,
            """
systemctl is-active bgp-agent || true
journalctl -u bgp-agent -n 40 --no-pager | tail -25
ss -tlnp | grep -E 'bgp_agent|:179|:183' || true
""",
        )
    )

    print("\n=== 201: FRR 对 153.204 ===\n")
    print(
        run(
            H201,
            pw,
            f"""
vtysh -c 'show bgp neighbors 10.133.153.204' 2>/dev/null | head -35
ss -tnp | grep 153.204 | head -8
""",
            timeout=60,
        )
    )

    print("\n=== 200: toggle 下游 + RR config ===\n")
    print(
        run(
            H200,
            pw,
            f"""
curl -sf -X POST http://127.0.0.1:9179/api/rr/config -H 'Content-Type: application/json' \\
  -d '{{"address":"{RR}","remote_as":63199,"local_address":"{LOCAL_RR}"}}'
curl -sf -X POST http://127.0.0.1:9179/api/rr/toggle -H 'Content-Type: application/json' \\
  -d '{{"address":"{RR}","enabled":true}}'
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle -H 'Content-Type: application/json' \\
  -d '{{"address":"{PEER}","vrf":"{VRF}","enabled":false}}'
sleep 2
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/toggle -H 'Content-Type: application/json' \\
  -d '{{"address":"{PEER}","vrf":"{VRF}","enabled":true}}'
curl -sf -X POST http://127.0.0.1:9179/api/rr/unfreeze
echo
""",
        )
    )

    print("等待 30s…")
    time.sleep(30)

    print("\n=== 200: 结果 ===\n")
    print(
        run(
            H200,
            pw,
            f"""
ss -tnp | grep -E '{PEER}|{RR}' | head -12
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('vrf')=='{VRF}' or n.get('address') in ('{PEER}','{RR}'):
    print(n.get('vrf'), n.get('address'), n.get('state'), 'rcvd', n.get('pfx_rcd'))
"
curl -sf http://127.0.0.1:9179/api/peers/freeze-status | python3 -c "
import json,sys
j=json.load(sys.stdin)
print('upstream_any_up', j.get('upstream_any_up'))
"
""",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
