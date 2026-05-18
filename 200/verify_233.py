#!/usr/bin/env python3
"""验收 10.133.152.233 卫星 BGP（部署后）。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

LAB = Path(__file__).resolve().parent
SPOOF = "10.133.152.233"
VRF = "vbgp10133152233"
PEER = "10.133.152.204"


def load_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def bash(c: paramiko.SSHClient, script: str) -> tuple[int, str]:
    i, o, e = c.exec_command("bash -se", timeout=90)
    i.write(script)
    i.channel.shutdown_write()
    out = o.read().decode(errors="replace") + e.read().decode(errors="replace")
    return o.channel.recv_exit_status(), out


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> None:
    load_env()
    host = os.environ["MTR_OP_HOST"]
    pw = os.environ["MTR_OP_SSH_PASSWORD"]
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)

    all_ok = True
    print(f"=== 验收 {SPOOF} @ {host} ===\n")

    code, out = bash(
        c,
        f"""
set -e
echo '--- kernel ---'
ip -br addr show iv233 2>/dev/null || true
ip route show vrf {VRF} 2>/dev/null || true
nft list table inet mtr_bgp_sat_dnat 2>/dev/null | head -20 || echo 'nft: no mtr_bgp_sat_dnat'
echo '--- agent ---'
curl -sf http://127.0.0.1:9179/api/neighbors
echo
curl -sf http://127.0.0.1:9179/api/peers/freeze-status
echo
echo '--- op ---'
curl -sf http://127.0.0.1:8808/api/bgp/neighbors
echo
ss -tnp state established 2>/dev/null | grep -E '{SPOOF}|{PEER}' || true
""",
    )
    print(out)

    all_ok &= check("ssh", code == 0, f"exit={code}")

    try:
        for line in out.splitlines():
            if line.strip().startswith("{") and "neighbors" in line:
                agent = json.loads(line)
                break
        else:
            agent = {}
        n233 = [
            n
            for n in agent.get("neighbors", [])
            if n.get("vrf") == VRF and str(n.get("address")) == PEER
        ]
        if n233:
            loc = n233[0].get("local_address") or n233[0].get("localAddress") or ""
            st = (n233[0].get("state") or "").upper()
            all_ok &= check("agent local", SPOOF in str(loc), str(loc))
            all_ok &= check("agent state", st == "ESTABLISHED", st)
        else:
            all_ok &= check("agent neighbor vbgp233", False, "not found")
    except json.JSONDecodeError:
        all_ok &= check("agent json", False)

    freeze_est = f'"vrf":"{VRF}"' in out and '"established":true' in out.replace(" ", "")
    all_ok &= check("freeze established", freeze_est)

    op_est = f'"source_ip":"{SPOOF}"' in out.replace(" ", "") and '"session_state":"Established"' in out
    all_ok &= check("op source+established", op_est)

    all_ok &= check("iv233 addr", SPOOF in out and "iv233" in out)
    all_ok &= check("vrf route peer", PEER in out and "iv233" in out)
    all_ok &= check("tcp bind iv233", f"{SPOOF}%iv233" in out or f"{SPOOF}" in out and ":179" in out)

    c.close()
    print("\n" + ("全部通过" if all_ok else "存在失败项"))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
