#!/usr/bin/env python3
"""排查 gobgp-rr / 10.133.153.204 RR 会话未 Established。"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
HOST = "10.133.151.200"
PW = "1234qwer"
RR = "10.133.153.204"
LOCAL = "10.133.153.200"


def load_env() -> str:
    pw = PW
    global HOST
    if (LAB / "lab.env").is_file():
        for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()
        pw = os.environ.get("MTR_OP_SSH_PASSWORD", pw)
        HOST = os.environ.get("MTR_OP_HOST", HOST)
    return pw


def main() -> int:
    pw = load_env()
    script = textwrap.dedent(
        f"""
        set -x
        echo '=== OP neighbor ==='
        curl -s http://127.0.0.1:8808/api/bgp/neighbors | python3 -c "
        import json,sys
        for r in json.load(sys.stdin):
          if r.get('neighbor_ip')=='{RR}':
            print(json.dumps(r,indent=2,ensure_ascii=False))
        "
        echo '=== Agent RR status ==='
        curl -s http://127.0.0.1:9179/api/rr/status | python3 -m json.tool 2>/dev/null || curl -s http://127.0.0.1:9179/api/rr/status
        echo
        echo '=== Agent neighbors RR ==='
        curl -s http://127.0.0.1:9179/api/neighbors | python3 -c "
        import json,sys
        for n in json.load(sys.stdin).get('neighbors',[]):
          if n.get('vrf')=='gobgp-rr' or n.get('address')=='{RR}':
            print(n)
        "
        echo '=== ping RR ==='
        ping -c2 -W2 -I {LOCAL} {RR} 2>&1 || ping -c2 -W2 {RR} 2>&1 || true
        echo '=== route to RR ==='
        ip route get {RR} from {LOCAL} 2>&1 || ip route get {RR} 2>&1 || true
        echo '=== BGP sockets 179 ==='
        ss -tnp | grep -E '179|153.204|153.200' | head -20 || true
        echo '=== bgp-agent env ==='
        cat /var/lib/bgp_agent/bgp-agent.env 2>/dev/null || true
        systemctl show bgp-agent -p ExecStart --no-pager 2>/dev/null | head -3
        echo '=== journal RR ==='
        journalctl -u bgp-agent -n 50 --no-pager 2>/dev/null | grep -iE 'rr|204|153|peer|error|fail|estab' | tail -30
        """
    )
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    _, stdout, stderr = c.exec_command(script, timeout=90)
    print(stdout.read().decode("utf-8", "replace"))
    err = stderr.read().decode("utf-8", "replace")
    if err.strip():
        print(err, file=sys.stderr)
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
