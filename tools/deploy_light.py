#!/usr/bin/env python3
"""
轻量部署现网 VR：上传 service/ + te_rewrite_nfqueue.py，保留 data.db，重启 uvicorn。

TE 改写由 uvicorn 启动时 te_rewrite_sync 按 hijack_enabled 拉起（路径 A，仅改真实 TE 外层源）。
已移除 mtr_spoof_nfqueue（路径 B）。

bgp-agent 默认本机预编译上传（不在 VR 上 go build）。改 Go 后：
  python tools/bgp_agent_build.py
  python tools/deploy_light.py

用法（仓库根目录）：
  pip install paramiko
  set MTR_OP_HOST=101.89.68.109
  set MTR_OP_SSH_PASSWORD=<密码>
  python tools/deploy_light.py
  python tools/deploy_light.py --op-only      # 仅 mtr-op
  python tools/deploy_light.py --agent-only  # 仅 bgp-agent（本地编译+上传+restart）

环境变量：MTR_DEPLOY_OP_ONLY=1 / MTR_DEPLOY_AGENT_ONLY=1
"""
from __future__ import annotations

import argparse
import os
import posixpath
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from bgp_agent_build import (  # noqa: E402
    bgp_agent_binary,
    build_bgp_agent_for_deploy,
    prebuilt_deploy_enabled,
    remote_rebuild_enabled,
    should_run_local_build,
)
from bgp_agent_remote import (  # noqa: E402
    bgp_agent_config_from_env,
    deploy_exec_timeout,
    shell_sync_bgp_agent,
)

REMOTE = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
SKIP_DIRS = {"venv", ".venv", "__pycache__", ".git", ".idea"}

def connect(host: str) -> paramiko.SSHClient:
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not pw:
        print("请设置 MTR_OP_SSH_PASSWORD（或 109/env）", file=sys.stderr)
        raise SystemExit(2)
    user = os.environ.get("MTR_OP_SSH_USER", "root").strip()
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


def run_script(c: paramiko.SSHClient, script: str, timeout: int = 180) -> tuple[int, str]:
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=timeout)
    stdout.channel.settimeout(timeout)
    stderr.channel.settimeout(timeout)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    return code, out + err


def ensure_dir(sftp: paramiko.SFTPClient, path: str) -> None:
    parts = path.strip("/").split("/")
    cur = ""
    for part in parts:
        cur += "/" + part
        try:
            sftp.mkdir(cur)
        except OSError:
            pass


def upload_tree(
    sftp: paramiko.SFTPClient,
    local_dir: Path,
    remote_dir: str,
    *,
    include_binary: bool = False,
) -> None:
    ensure_dir(sftp, remote_dir)
    for p in local_dir.iterdir():
        if p.name in SKIP_DIRS or p.name.endswith(".db") or p.name.endswith(".pyc"):
            continue
        rp = posixpath.join(remote_dir, p.name)
        if p.is_dir():
            if p.name == "bgp_agent":
                continue
            upload_tree(sftp, p, rp, include_binary=include_binary)
        else:
            sftp.put(str(p), rp)

    bgp_dir = local_dir / "bgp_agent"
    if bgp_dir.is_dir():
        upload_bgp_agent_tree(
            sftp,
            bgp_dir,
            posixpath.join(remote_dir, "bgp_agent"),
            include_binary=include_binary,
        )


def upload_bgp_agent_tree(
    sftp: paramiko.SFTPClient,
    local_dir: Path,
    remote_dir: str,
    *,
    include_binary: bool = False,
) -> None:
    ensure_dir(sftp, remote_dir)
    for p in local_dir.rglob("*"):
        if p.is_dir():
            continue
        if not include_binary and (
            p.name in {"bgp_agent", "bgp_agent.exe"} or p.suffix == ".exe"
        ):
            continue
        rel = p.relative_to(local_dir).as_posix()
        rp = posixpath.join(remote_dir, rel)
        parent = posixpath.dirname(rp)
        if parent:
            ensure_dir(sftp, parent)
        sftp.put(str(p), rp)


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() and k.strip() not in os.environ:
            os.environ[k.strip()] = v.strip()


def op_only_deploy() -> bool:
    raw = os.environ.get("MTR_DEPLOY_OP_ONLY", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def agent_only_deploy() -> bool:
    raw = os.environ.get("MTR_DEPLOY_AGENT_ONLY", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def main() -> None:
    ap = argparse.ArgumentParser(description="轻量部署 mtr-op（可选仅 OP / 仅 Agent）")
    ap.add_argument(
        "--op-only",
        action="store_true",
        help="仅上传 service 应用与 TE 脚本，重启 uvicorn；不触碰 bgp-agent",
    )
    ap.add_argument(
        "--agent-only",
        action="store_true",
        help="仅本机编译并上传 bgp_agent 二进制，重启 bgp-agent（不重启 uvicorn）",
    )
    args = ap.parse_args()
    op_only = args.op_only or op_only_deploy()
    agent_only = args.agent_only or agent_only_deploy()
    if op_only and agent_only:
        print("不能同时 --op-only 与 --agent-only", file=sys.stderr)
        sys.exit(2)

    load_env_file(ROOT / "109" / "env")
    service_dir = ROOT / "service"
    te_nfq = ROOT / "scripts" / "te_rewrite_nfqueue.py"
    nft_admin_acl = ROOT / "scripts" / "nft_mtr_admin_acl.nft"
    if not agent_only:
        if not service_dir.is_dir():
            print("missing service/", file=sys.stderr)
            sys.exit(1)
        if not te_nfq.is_file():
            print("missing scripts/te_rewrite_nfqueue.py", file=sys.stderr)
            sys.exit(1)

    use_prebuilt = agent_only or (not op_only and prebuilt_deploy_enabled(ROOT))
    if agent_only or (not op_only and use_prebuilt):
        print("  本地编译 bgp_agent ...", flush=True)
        build_bgp_agent_for_deploy(ROOT)
        if not bgp_agent_binary(ROOT).is_file():
            print("未找到 service/bgp_agent/bgp_agent", file=sys.stderr)
            sys.exit(1)
        print(f"  binary: {bgp_agent_binary(ROOT)} ({bgp_agent_binary(ROOT).stat().st_size} bytes)", flush=True)
    elif not op_only:
        if use_prebuilt and should_run_local_build(ROOT):
            build_bgp_agent_for_deploy(ROOT)
        elif use_prebuilt and not bgp_agent_binary(ROOT).is_file():
            print("未找到 service/bgp_agent/bgp_agent，请先本地编译:", file=sys.stderr)
            print("  python tools/bgp_agent_build.py", file=sys.stderr)
            sys.exit(1)

    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    user = os.environ.get("MTR_OP_SSH_USER", "root").strip()
    print(f"Connecting {user}@{host} ...", flush=True)
    if agent_only:
        print("  mode: agent-only (upload bgp_agent + restart bgp-agent)", flush=True)
    elif op_only:
        print("  mode: op-only (skip bgp-agent stop/upload/restart)", flush=True)
    elif use_prebuilt:
        print("  bgp-agent: upload local binary (no remote compile)", flush=True)
    c = connect(host)
    if use_prebuilt and not op_only:
        code, out = run_script(
            c,
            "systemctl stop bgp-agent 2>/dev/null || true; pkill -f bgp_agent 2>/dev/null || true; sleep 1; echo stopped",
            timeout=30,
        )
        print(out, end="")
        if code != 0:
            print(f"WARN: stop bgp-agent exit={code}", flush=True)
    sftp = c.open_sftp()
    try:
        if agent_only:
            remote_bin = posixpath.join(REMOTE, "bgp_agent", "bgp_agent")
            ensure_dir(sftp, posixpath.join(REMOTE, "bgp_agent"))
            sftp.put(str(bgp_agent_binary(ROOT)), remote_bin)
            print(f"  uploaded -> {remote_bin}", flush=True)
        else:
            upload_tree(sftp, service_dir, REMOTE, include_binary=use_prebuilt and not op_only)
            sftp.put(str(te_nfq), f"{REMOTE}/te_rewrite_nfqueue.py")
            if nft_admin_acl.is_file():
                sftp.put(str(nft_admin_acl), f"{REMOTE}/nft_mtr_admin_acl.nft")
    finally:
        sftp.close()

    if agent_only:
        bgp_cfg = bgp_agent_config_from_env()
        bgp_cfg["remote_dir"] = REMOTE
        restart = shell_sync_bgp_agent(bgp_cfg, rebuild=False)
        ssh_timeout = deploy_exec_timeout(remote_rebuild=False)
        code, out = run_script(c, restart, timeout=ssh_timeout)
        print(out, end="")
        c.close()
        if code != 0:
            sys.exit(code)
        print("deploy_light_ok (agent-only)", flush=True)
        return

    restart = f"""
set -e
cd {REMOTE}
export GOBGP_AGENT_URL=http://127.0.0.1:9179
export MTR_OP_DB={REMOTE}/data.db
export MTR_OP_NFT={REMOTE}/nft_mtr_te.nft
export MTR_OP_DATA={REMOTE}/data
export MTR_BGP_IPVLAN_AUTO=1
export MTR_BGP_IPVLAN_BASE_IFACE={os.environ.get("MTR_BGP_IPVLAN_BASE_IFACE", "eno1np0").strip()}
export MTR_BGP_RR_UPLINK_IFACE={os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0").strip()}
export MTR_OP_DOWNSTREAM_IFACE={os.environ.get("MTR_OP_DOWNSTREAM_IFACE", os.environ.get("MTR_BGP_IPVLAN_BASE_IFACE", "eno1np0")).strip()}
export MTR_TE_REWRITE_OIF={os.environ.get("MTR_TE_REWRITE_OIF", os.environ.get("MTR_BGP_IPVLAN_BASE_IFACE", "eno1np0")).strip()}
export MTR_TE_REWRITE_IIF={os.environ.get("MTR_TE_REWRITE_IIF", os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0")).strip()}
export MTR_BGP_RR_SPOOF_IPVLAN_ADDR={os.environ.get("MTR_BGP_RR_SPOOF_IPVLAN_ADDR", "0").strip()}
export MTR_BGP_IPVLAN_PEER_IP={os.environ.get("MTR_BGP_IPVLAN_PEER_IP", "139.159.43.208").strip()}
export RR_ADDR={os.environ.get("RR_ADDR", "139.159.43.249").strip()}
if [ -x ./venv/bin/python ]; then PY=./venv/bin/python; else PY=python3; fi
$PY - <<'INITSCHEMA'
import os, sys
sys.path.insert(0, "{REMOTE}")
from pathlib import Path
from app import storage
db = Path(os.environ["MTR_OP_DB"])
conn = storage.connect(db)
storage.init_schema(conn)
conn.close()
print("schema_ok")
INITSCHEMA
pkill -f 'uvicorn app.main:app' 2>/dev/null || true
pkill -f mtr_spoof_nfqueue 2>/dev/null || true
sleep 1
nft delete table inet mtr_spoof 2>/dev/null || true
nft delete table inet mtr_te 2>/dev/null || true
[ -f nft_mtr_te.nft ] && nft -f nft_mtr_te.nft || echo "WARN: nft load skipped"
: > /tmp/mtr_op.log
if [ -x ./venv/bin/uvicorn ]; then UV=./venv/bin/uvicorn; else UV='python3 -m uvicorn'; fi
nohup env MTR_OP_DB="$MTR_OP_DB" MTR_OP_NFT="$MTR_OP_NFT" \\
  MTR_BGP_IPVLAN_BASE_IFACE="$MTR_BGP_IPVLAN_BASE_IFACE" \\
  MTR_BGP_RR_UPLINK_IFACE="$MTR_BGP_RR_UPLINK_IFACE" \\
  MTR_OP_DOWNSTREAM_IFACE="$MTR_OP_DOWNSTREAM_IFACE" \\
  MTR_TE_REWRITE_OIF="$MTR_TE_REWRITE_OIF" MTR_TE_REWRITE_IIF="$MTR_TE_REWRITE_IIF" \\
  MTR_TE_REWRITE_SCRIPT={REMOTE}/te_rewrite_nfqueue.py \\
  $UV app.main:app --host 0.0.0.0 --port 8808 >> /tmp/mtr_op.log 2>&1 &
sleep 5
for i in 1 2 3 4 5 6 7 8 9 10; do
  curl -sf http://127.0.0.1:8808/health >/dev/null && break
  sleep 1
done
curl -sS http://127.0.0.1:8808/health; echo
echo '=== mangle FORWARD NFQUEUE (expect eno1np0 / enp59s0f0np0) ==='
iptables -t mangle -S FORWARD 2>/dev/null | grep -E 'NFQUEUE|eno1np0|enp59s0f0np0|ens192|ens224' || true
pgrep -af 'uvicorn|te_rewrite|mtr_spoof' || true
"""
    if not op_only:
        rebuild_bgp = False if use_prebuilt else remote_rebuild_enabled()
        bgp_cfg = bgp_agent_config_from_env()
        bgp_cfg["remote_dir"] = REMOTE
        restart += shell_sync_bgp_agent(bgp_cfg, rebuild=rebuild_bgp)
        ssh_timeout = deploy_exec_timeout(remote_rebuild=rebuild_bgp)
    else:
        ssh_timeout = deploy_exec_timeout(remote_rebuild=False)
    code, out = run_script(c, restart, timeout=ssh_timeout)
    print(out, end="")
    c.close()
    if code != 0:
        sys.exit(code)
    print("deploy_light_ok", flush=True)


if __name__ == "__main__":
    main()
