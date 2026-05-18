#!/usr/bin/env python3
"""恢复 RR（RX）：删除 gobgp-rr 上误建的 TX，再 /api/rr/config。"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
RR = "10.133.153.204"
LOCAL = "10.133.153.200"
RR_AS = 63199


def load_env() -> tuple[str, str]:
    host = "10.133.151.200"
    pw = "1234qwer"
    if (LAB / "lab.env").is_file():
        for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()
        host = os.environ.get("MTR_OP_HOST", host)
        pw = os.environ.get("MTR_OP_SSH_PASSWORD", pw)
        global RR, LOCAL, RR_AS
        RR = os.environ.get("RR_ADDR", RR)
        LOCAL = os.environ.get("ROUTER_ID", LOCAL)
        RR_AS = int(os.environ.get("RR_AS", RR_AS))
    return host, pw


def main() -> int:
    host, pw = load_env()
    body = (
        f'{{"vrf":"gobgp-rr","address":"{RR}"}}'
    )
    cfg = (
        f'{{"address":"{RR}","remote_as":{RR_AS},'
        f'"local_address":"{LOCAL}"}}'
    )
    script = textwrap.dedent(
        f"""
        set -e
        echo '=== remove stray TX on gobgp-rr ==='
        curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove \\
          -H 'Content-Type: application/json' -d '{body}' || echo '(no tx peer)'
        echo
        echo '=== configure RX RR ==='
        curl -sf -X POST http://127.0.0.1:9179/api/rr/config \\
          -H 'Content-Type: application/json' -d '{cfg}'
        echo
        sleep 3
        echo '=== RR status ==='
        curl -s http://127.0.0.1:9179/api/rr/status | python3 -m json.tool
        echo
        echo '=== neighbors (RR) ==='
        curl -s http://127.0.0.1:9179/api/neighbors | python3 -c "
        import json,sys
        for n in json.load(sys.stdin).get('neighbors',[]):
          if n.get('vrf')=='gobgp-rr' or n.get('address')=='{RR}':
            print(n)
        "
        echo '=== BGP sockets ==='
        ss -tnp | grep -E '179|{RR}' | head -15 || true
        """
    )
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    _, stdout, stderr = c.exec_command(script, timeout=120)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    print(out)
    if err.strip():
        print(err, file=sys.stderr)
    c.close()
    if "ESTABLISHED" in out.upper() or '"state":"BGP_FSM_ESTABLISHED"' in out.replace(" ", ""):
        print("OK: RR session looks established")
        return 0
    if '"rr_addr":"{RR}"' in out or f'"rr_addr": "{RR}"' in out:
        print("WARN: RR configured but not yet Established — check uplink/ping")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
