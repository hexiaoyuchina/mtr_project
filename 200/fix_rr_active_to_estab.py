#!/usr/bin/env python3
"""排查并修复 gobgp-rr 153.200<->153.204 长期 Active。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
RR, LOCAL = "10.133.153.204", "10.133.153.200"


def load_env() -> str:
    pw = "1234qwer"
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    return os.environ.get("MTR_OP_SSH_PASSWORD", pw)


def ros(pw: str, cmd: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.133.151.210", username="admin", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command(cmd, timeout=90)
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def root(pw: str, script: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(os.environ.get("MTR_OP_HOST", "10.133.151.200"), username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=180)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    pw = load_env()
    print("=== ROS peer 状态 ===\n")
    print(ros(pw, "/routing bgp peer print detail where name=peer-lin200-153"))
    print(ros(pw, "/routing bgp peer print where remote-address=10.133.153.200"))

    print("\n=== 200 诊断 ===\n")
    print(
        root(
            pw,
            f"""
set -x
bash /root/mtr_op/remote-network-prereq.sh 2>/dev/null || true
echo '--- ping ---'
ping -c2 -W2 -I {LOCAL} {RR} 2>&1 || true
echo '--- route ---'
ip route get {RR} from {LOCAL} 2>&1 || ip route get {RR} 2>&1
echo '--- ss 179 ---'
ss -tnp | grep -E '{LOCAL}|{RR}' | grep 179 || echo no_bgp_sock
echo '--- agent rr ---'
curl -s http://127.0.0.1:9179/api/rr/status | python3 -m json.tool 2>/dev/null | head -25
curl -s http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='{RR}':
    print(json.dumps(n,indent=2))
"
echo '--- journal ---'
journalctl -u bgp-agent -n 40 --no-pager | grep -iE '153.204|153.200|peer|error|Active|estab' | tail -20
""",
        )
    )

    print("\n=== ROS：重建 peer-lin200-153 ===\n")
    print(ros(pw, "/routing bgp peer remove [find name=peer-lin200-153]"))
    print(
        ros(
            pw,
            '/routing bgp peer add name=peer-lin200-153 remote-address=10.133.153.200 remote-as=63199 '
            'update-source=10.133.153.204 address-families=ip',
        )
    )
    print(ros(pw, "/routing bgp peer print detail where name=peer-lin200-153"))

    print("\n=== 200：RR 配置 + unfreeze ===\n")
    print(
        root(
            pw,
            f"""
curl -sf -X POST http://127.0.0.1:9179/api/rr/toggle -H 'Content-Type: application/json' -d '{{"address":"{RR}","enabled":true}}'
curl -sf -X POST http://127.0.0.1:9179/api/rr/config -H 'Content-Type: application/json' \\
  -d '{{"address":"{RR}","remote_as":63199,"local_address":"{LOCAL}"}}'
curl -sf -X POST http://127.0.0.1:9179/api/rr/unfreeze
""",
        )
    )

    print("\n等待 30s …")
    time.sleep(30)
    print(ros(pw, "/routing bgp peer print detail where name=peer-lin200-153"))
    print(
        root(
            pw,
            f"""
ss -tnp state established | grep -E '{LOCAL}|{RR}' | grep 179 || echo no_estab
curl -s http://127.0.0.1:9179/api/rr/status | python3 -m json.tool 2>/dev/null | head -20
curl -s http://127.0.0.1:8808/api/bgp/neighbors | python3 -c "
import json,sys
for r in json.load(sys.stdin):
  if r.get('neighbor_ip')=='{RR}':
    print('OP:', r.get('session_state'), 'rcvd', r.get('routes_received'), 'cached', r.get('routes_cached'))
"
""",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
