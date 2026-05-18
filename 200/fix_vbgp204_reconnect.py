#!/usr/bin/env python3
"""部署 SetNeighborEnabled 修复并在 200 上恢复 vbgp10133153204 会话。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
ROOT = LAB.parent
HOST = "10.133.151.201"


def load_env() -> str:
    pw = "1234qwer"
    env = LAB / "lab.env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()
        pw = os.environ.get("MTR_OP_SSH_PASSWORD", pw)
        global HOST
        HOST = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    return pw


def main() -> int:
    pw = load_env()
    sys.path.insert(0, str(ROOT / "tools"))
    from bgp_agent_remote import bgp_agent_config_from_env, shell_sync_bgp_agent  # noqa: E402

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    remote_tx = f"{os.environ.get('MTR_OP_REMOTE_DIR', '/root/mtr_op')}/bgp_agent/pkg/tx/tx_agent.go"
    sftp.put(str(ROOT / "service/bgp_agent/pkg/tx/tx_agent.go"), remote_tx)
    sftp.put(str(LAB / "fix_vbgp204_reconnect.sh"), "/root/fix_vbgp204_reconnect.sh")
    sftp.close()

    cfg = bgp_agent_config_from_env()
    script = shell_sync_bgp_agent(cfg, rebuild=True)
    script += "\nchmod +x /root/fix_vbgp204_reconnect.sh\nbash /root/fix_vbgp204_reconnect.sh\n"
    _, stdout, stderr = c.exec_command(script, timeout=300)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    c.close()
    print(out)
    if err.strip():
        print(err, file=sys.stderr)
    return 0 if "ESTABLISHED" in out or "Established" in out else 1


if __name__ == "__main__":
    sys.exit(main())
