#!/usr/bin/env python3
"""
现网 109：远端清理 → 条件安装 → 全量 deploy_bgp_rxtx（不执行请仅审 env.example）。

用法（仓库根目录）：
  cp 109/env.example 109/env   # 填 MTR_OP_SSH_PASSWORD
  pip install paramiko
  python 109/deploy_fresh.py
  python 109/deploy_fresh.py --skip-clean --skip-bootstrap   # 仅重跑全量部署
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

DEPLOY_DIR = Path(__file__).resolve().parent
ROOT = DEPLOY_DIR.parent
DEPLOY_BGP = ROOT / "service" / "scripts" / "deploy_bgp_rxtx.py"

ENV_KEYS = (
    "MTR_OP_HOST",
    "MTR_OP_SSH_USER",
    "MTR_OP_SSH_PASSWORD",
    "MTR_OP_REMOTE_DIR",
    "MTR_OP_PORT",
    "MTR_PROBE_SSH_HOST",
    "LOCAL_AS",
    "RR_AS",
    "ROUTER_ID",
    "RR_ADDR",
    "MTR_DOWNSTREAM_REMOTE_AS",
    "MTR_SATELLITE_PEER_IP",
    "MTR_OP_DOWNSTREAM_IFACE",
    "MTR_BGP_RR_UPLINK_IFACE",
    "MTR_BGP_RR_SPOOF_IPVLAN_ADDR",
    "MTR_BGP_IPVLAN_BASE_IFACE",
    "MTR_BGP_IPVLAN_PEER_IP",
    "MTR_BGP_IPVLAN_AUTO",
    "MTR_BGP_ROLE_MAP",
    "MTR_BGP_DB_PRESETS",
    "REDIS_ADDR",
    "ROCKSDB_PATH",
    "API_ADDR",
    "MTR_DEPLOY_BUILD_BGP_AGENT",
    "MTR_DEPLOY_BGP_AGENT_PREBUILT",
    "MTR_DEPLOY_BGP_AGENT_LOCAL_BUILD",
    "MTR_OP_SKIP_INSTALL",
    "MTR_OP_PRESERVE_DIR",
)


def load_env() -> None:
    for name in ("env", "env.example"):
        env_file = DEPLOY_DIR / name
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            key = k.strip()
            if key and key not in os.environ:
                os.environ[key] = v.strip()
        if name == "env":
            break


def connect() -> paramiko.SSHClient:
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    user = os.environ.get("MTR_OP_SSH_USER", "root").strip()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not pw:
        print("请填写 109/env 中的 MTR_OP_SSH_PASSWORD", file=sys.stderr)
        raise SystemExit(2)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        host,
        username=user,
        password=pw,
        timeout=30,
        banner_timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    return c


def run_remote(c: paramiko.SSHClient, script: str, timeout: int = 1200) -> tuple[int, str]:
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=timeout)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    return code, out + err


def upload_and_run_script(
    c: paramiko.SSHClient, local_name: str, remote_name: str, env_exports: str
) -> None:
    local = DEPLOY_DIR / local_name
    remote_dir = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").strip()
    remote_path = f"{remote_dir}/{remote_name}"
    sftp = c.open_sftp()
    try:
        try:
            sftp.stat(remote_dir)
        except OSError:
            sftp.mkdir(remote_dir)
        sftp.put(str(local), remote_path)
    finally:
        sftp.close()
    code, out = run_remote(
        c,
        f"""set -e
{env_exports}
chmod +x {remote_path}
bash {remote_path}
""",
    )
    print(out, end="")
    if code != 0:
        print(f"{local_name} failed exit={code}", file=sys.stderr)
        raise SystemExit(code)


def env_exports() -> str:
    lines = []
    for key in ENV_KEYS:
        val = os.environ.get(key, "")
        if val:
            lines.append(f'export {key}="{val}"')
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="现网 109 干净全量部署")
    parser.add_argument("--skip-clean", action="store_true", help="跳过 remote-clean-fresh.sh")
    parser.add_argument("--skip-bootstrap", action="store_true", help="跳过 remote-bootstrap.sh")
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="传给 deploy_bgp_rxtx（MTR_OP_SKIP_INSTALL=1）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印将执行步骤，不 SSH、不部署",
    )
    args = parser.parse_args()

    load_env()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109")
    print(f"=== deploy_fresh target {host} ===")

    if args.skip_install:
        os.environ["MTR_OP_SKIP_INSTALL"] = "1"

    if args.dry_run:
        print("DRY-RUN:")
        print(f"  skip_clean={args.skip_clean} skip_bootstrap={args.skip_bootstrap}")
        print(f"  skip_install={args.skip_install}")
        print(f"  then: {DEPLOY_BGP}")
        return

    if not DEPLOY_BGP.is_file():
        print(f"缺少 {DEPLOY_BGP}", file=sys.stderr)
        raise SystemExit(1)

    exp = env_exports()
    c = connect()
    try:
        if not args.skip_clean:
            print("\n=== 1. remote-clean-fresh ===")
            upload_and_run_script(c, "remote-clean-fresh.sh", "remote-clean-fresh.sh", exp)
        if not args.skip_bootstrap:
            print("\n=== 2. remote-bootstrap ===")
            upload_and_run_script(c, "remote-bootstrap.sh", "remote-bootstrap.sh", exp)
    finally:
        c.close()

    print("\n=== 3. deploy_bgp_rxtx.py ===")
    env = os.environ.copy()
    for key in ENV_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    r = subprocess.run([sys.executable, str(DEPLOY_BGP)], env=env, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(r.returncode)

    print("\ndeploy_fresh_ok — 请审核 Web 邻居/ARP 后执行: python 109/verify.py")


if __name__ == "__main__":
    main()
