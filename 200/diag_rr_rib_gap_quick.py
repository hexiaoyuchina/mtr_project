#!/usr/bin/env python3
"""快速复查 RIB 差距（ingest 超时后）。"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
RR, VRF = "10.133.153.204", "gobgp-rr"


def main() -> int:
    pw = "1234qwer"
    host = "10.133.151.200"
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", pw)
    host = os.environ.get("MTR_OP_HOST", host)

    script = textwrap.dedent(
        f"""
        echo '=== counts now ==='
        curl -sf 'http://127.0.0.1:9179/api/rib/routes/count?window=upstream&vrf={VRF}&neighbor_ip={RR}'
        echo
        curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
        import json,sys
        for n in json.load(sys.stdin).get('neighbors',[]):
          if n.get('address')=='{RR}': print('pfx_rcd', n.get('pfx_rcd'), 'state', n.get('state'))
        "
        echo '=== ingest running? ==='
        ps aux | grep -E 'bgp_agent|ingest' | grep -v grep | head -5
        echo '=== recent journal ==='
        journalctl -u bgp-agent -n 30 --no-pager 2>/dev/null | tail -20
        echo '=== rocksdb dir size ==='
        du -sh /var/lib/bgp_agent/rocksdb 2>/dev/null || true
        """
    )
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=90)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    print((o.read() + e.read()).decode("utf-8", "replace"))
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
