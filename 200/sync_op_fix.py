#!/usr/bin/env python3
"""上传已修复的 OP 代码到 Linux 200 并重启（不碰 data.db）。"""
from __future__ import annotations

import os
import posixpath
import sys
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
ROOT = LAB.parent
SERVICE = ROOT / "service"

FILES = [
    "app/storage.py",
    "app/main.py",
    "app/bgp_peer_rib.py",
    "app/bgp_control.py",
    "app/bgp_ipvlan_reconcile.py",
    "app/satellite_vrf_assign.py",
    "app/vrf_naming.py",
    "static/index.html",
]


def load_lab_env() -> None:
    env_file = LAB / "lab.env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()


def main() -> int:
    load_lab_env()
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
    if not pw:
        print("MTR_OP_SSH_PASSWORD required", file=sys.stderr)
        return 2

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    try:
        for rel in FILES:
            lp = SERVICE / rel
            rp = posixpath.join(remote, rel.replace("\\", "/"))
            sftp.put(str(lp), rp)
            print(f"upload {rel} -> {rp}")
        sftp.put(str(LAB / "remote-restart.sh"), f"{remote}/remote-restart.sh")
        print("upload remote-restart.sh")
    finally:
        sftp.close()

    script = f"export MTR_OP_REMOTE_DIR={remote}\n"
    for key in (
        "LOCAL_AS",
        "ROUTER_ID",
        "MTR_DOWNSTREAM_REMOTE_AS",
        "MTR_BGP_IPVLAN_AUTO",
        "MTR_BGP_IPVLAN_BASE_IFACE",
        "MTR_BGP_RR_UPLINK_IFACE",
        "MTR_BGP_IPVLAN_PEER_IP",
        "MTR_SATELLITE_PEER_IP",
        "MTR_SATELLITE_PHY_VRF",
        "MTR_AUTO_SATELLITE_VRF",
        "MTR_AUTO_SATELLITE_VRF_NOTE",
        "MTR_BGP_ROLE_MAP",
        "MTR_BGP_DB_PRESETS",
        "MTR_PROBE_SSH_HOST",
    ):
        val = os.environ.get(key, "")
        if val:
            script += f'export {key}="{val}"\n'
    script += f"bash {remote}/remote-restart.sh\n"
    script += (
        "grep peer_norm "
        f"{remote}/app/bgp_ipvlan_reconcile.py | head -3\n"
    )

    stdin, stdout, stderr = c.exec_command("bash -se", timeout=120)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    print(out)
    c.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
