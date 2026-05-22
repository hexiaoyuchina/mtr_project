#!/usr/bin/env python3
"""仅同步 bgp-agent systemd 参数并 restart（不上传 OP 代码）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from bgp_agent_remote import (  # noqa: E402
    bgp_agent_config_from_env,
    deploy_exec_timeout,
    shell_sync_bgp_agent,
)

HOST = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
USER = os.environ.get("MTR_OP_SSH_USER", "root").strip()
PW = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
REMOTE = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").strip()


def main() -> None:
    if not PW:
        print("请设置 MTR_OP_SSH_PASSWORD", file=sys.stderr)
        sys.exit(2)
    rebuild = os.environ.get("MTR_DEPLOY_BUILD_BGP_AGENT", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    cfg = bgp_agent_config_from_env()
    cfg["remote_dir"] = REMOTE
    script = shell_sync_bgp_agent(cfg, rebuild=rebuild)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting {USER}@{HOST} ...", flush=True)
    c.connect(HOST, username=USER, password=PW, timeout=30, allow_agent=False, look_for_keys=False)
    ssh_timeout = deploy_exec_timeout(remote_rebuild=rebuild)
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=ssh_timeout)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = stdout.read().decode() + stderr.read().decode()
    code = stdout.channel.recv_exit_status()
    print(out, end="")
    c.close()
    if code != 0:
        sys.exit(code)
    print("sync_bgp_agent_ok", flush=True)


if __name__ == "__main__":
    main()
