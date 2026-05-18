#!/usr/bin/env python3
"""200/201/210：排查 gobgp-rr RR 会话（153.204 <- 153.200）。"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
PW = "1234qwer"
H200 = "10.133.151.200"
H201 = "10.133.151.201"
H210 = "10.133.151.210"
RR = "10.133.153.204"
LOCAL = "10.133.153.200"


def load_env() -> str:
    pw = PW
    if (LAB / "lab.env").is_file():
        for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()
        pw = os.environ.get("MTR_OP_SSH_PASSWORD", pw)
    return pw


def run(host: str, user: str, script: str, pw: str, timeout: int = 120) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(host, username=user, password=pw, timeout=30, allow_agent=False, look_for_keys=False)
        if user == "root":
            _, o, e = c.exec_command("bash -se", timeout=timeout)
            o.channel.send(script.encode())
            o.channel.shutdown_write()
        else:
            _, o, e = c.exec_command(script, timeout=timeout)
        out = o.read().decode("utf-8", "replace")
        err = e.read().decode("utf-8", "replace")
        return out + (("\n" + err) if err.strip() else "")
    except Exception as ex:
        return f"SSH FAIL: {ex}\n"
    finally:
        c.close()


def main() -> int:
    pw = load_env()
    s200 = textwrap.dedent(
        f"""
        set -x
        echo '======== OP :8808 neighbors RR ========'
        curl -sf http://127.0.0.1:8808/health && echo
        curl -s http://127.0.0.1:8808/api/bgp/neighbors | python3 -c "
        import json,sys
        for r in json.load(sys.stdin):
          if r.get('vrf')=='gobgp-rr' or r.get('neighbor_ip')=='{RR}':
            print(json.dumps(r,ensure_ascii=False))
        "
        echo '======== Agent :9179 ========'
        curl -s http://127.0.0.1:9179/api/rr/status | python3 -m json.tool
        echo '--- neighbors ---'
        curl -s http://127.0.0.1:9179/api/neighbors | python3 -c "
        import json,sys
        for n in json.load(sys.stdin).get('neighbors',[]):
          if n.get('vrf')=='gobgp-rr' or n.get('address')=='{RR}':
            print(n)
        "
        echo '--- freeze upstream ---'
        curl -s http://127.0.0.1:9179/api/peers/freeze-status | python3 -c "
        import json,sys; print(json.dumps(json.load(sys.stdin).get('upstream'),indent=2))
        "
        echo '--- network ---'
        ip rule list | grep -E '153.200|2103' || true
        ip route show table 2103 | grep -E '153.204|153.200' || true
        ping -c2 -W2 -I ens224 {RR} || true
        echo '--- BGP 179 sockets ---'
        ss -tnp | grep -E '153.204|153.200|:179' | head -25 || true
        echo '--- journal 153.204 ---'
        journalctl -u bgp-agent -n 100 --no-pager 2>/dev/null | grep -iE '153.204|Peer Up|Peer Down|fail|error' | tail -25 || true
        """
    )
    s201 = textwrap.dedent(
        f"""
        echo '======== Linux 201 ========'
        ip -br addr | head -20
        echo '--- FRR ---'
        systemctl is-active frr 2>/dev/null || true
        vtysh -c 'show bgp summary' 2>/dev/null | head -25 || echo 'no vtysh'
        vtysh -c 'show run' 2>/dev/null | grep -iE '153.204|153.200|63199|neighbor|router bgp' | head -40 || true
        echo '--- ping 153.204 / 153.200 ---'
        ping -c1 -W2 {RR} 2>&1 || true
        ping -c1 -W2 {LOCAL} 2>&1 || true
        ss -tnp | grep -E ':179|153.20' | head -15 || true
        """
    )
    s210 = f"""
echo '======== RouterOS 210 (RR {RR}) ========'
/ip address print where address~\"153.204\"
/routing bgp connection print detail where remote.address~\"153.200\" or local.address~\"153.200\"
/routing bgp connection print detail where remote.address~\"153.204\" or name~\"200\"
/routing bgp session print detail where remote.address~\"153.200\"
"""

    print("=" * 70)
    print(f"Linux 200 {H200}")
    print("=" * 70)
    print(run(H200, "root", s200, pw))

    print("=" * 70)
    print(f"Linux 201 {H201}")
    print("=" * 70)
    print(run(H201, "root", s201, pw))

    print("=" * 70)
    print(f"RouterOS 210 {H210} (RR BGP {RR})")
    print("=" * 70)
    try:
        print(run(H210, "admin", s210, pw))
    except Exception:
        print(run(H210, "admin", s210, "1234qwer"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
