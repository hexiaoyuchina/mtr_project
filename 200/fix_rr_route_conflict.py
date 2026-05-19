#!/usr/bin/env python3
"""修复 153.204 在 ens192/iv204 与 ens224 RR 冲突导致 gobgp-rr Active。"""
from __future__ import annotations

import os
import time
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
RR, LOCAL = "10.133.153.204", "10.133.153.200"


def load_env() -> str:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    return os.environ["MTR_OP_SSH_PASSWORD"]


def run(pw: str, script: str, timeout: int = 120) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(os.environ.get("MTR_OP_HOST", "10.133.151.200"), username="root", password=pw, timeout=45)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    return (o.read() + e.read()).decode("utf-8", "replace")


def main() -> int:
    pw = load_env()
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")

    # upload prereq script
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.133.151.200", username="root", password=pw, timeout=45)
    sftp = c.open_sftp()
    sftp.put(str(LAB / "remote-network-prereq.sh"), f"{remote}/remote-network-prereq.sh")
    sftp.close()
    c.close()

    print(
        run(
            pw,
            f"""
set -e
export RR_ADDR={RR} ROUTER_ID={LOCAL} MTR_BGP_RR_UPLINK_IFACE=ens224
bash {remote}/remote-network-prereq.sh
bash {remote}/ensure_uplink_addrs.sh 2>/dev/null || true
echo '--- route get (must be ens224 table 2103) ---'
ip route get {RR} from {LOCAL}
ip -4 rule show | grep -E '45:|50:|153'
echo '--- RR reconfig ---'
curl -sf -X POST http://127.0.0.1:9179/api/rr/config -H 'Content-Type: application/json' \\
  -d '{{"address":"{RR}","remote_as":63199,"local_address":"{LOCAL}"}}'
curl -sf -X POST http://127.0.0.1:9179/api/rr/toggle -H 'Content-Type: application/json' \\
  -d '{{"address":"{RR}","enabled":false}}'
sleep 2
curl -sf -X POST http://127.0.0.1:9179/api/rr/toggle -H 'Content-Type: application/json' \\
  -d '{{"address":"{RR}","enabled":true}}'
curl -sf -X POST http://127.0.0.1:9179/api/rr/unfreeze
curl -sf -X POST http://127.0.0.1:8808/api/gobgp/unfreeze
""",
        )
    )
    print("\n等待 25s…")
    time.sleep(25)
    print(
        run(
            pw,
            f"""
ip route get {RR} from {LOCAL}
ss -tnp | grep '{RR}.*179' | head -6
curl -sf http://127.0.0.1:9179/api/rr/status | python3 -m json.tool 2>/dev/null | head -18
""",
            timeout=40,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
