#!/usr/bin/env python3
"""
轻量部署现网 VR：上传 service/ + mtr_spoof_nfqueue.py，保留 data.db，重启 uvicorn 与 NFQUEUE。

用法（仓库根目录）：
  pip install paramiko
  set MTR_OP_HOST=101.89.68.109
  set MTR_OP_SSH_PASSWORD=<密码>
  python tools/deploy_light.py
"""
from __future__ import annotations

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
from bgp_agent_remote import bgp_agent_config_from_env, shell_sync_bgp_agent  # noqa: E402

REMOTE = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
SKIP_DIRS = {"venv", ".venv", "__pycache__", ".git", ".idea"}

HOST = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
USER = os.environ.get("MTR_OP_SSH_USER", "root").strip()
PW = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()


def connect(host: str) -> paramiko.SSHClient:
    if not PW:
        print("请设置环境变量 MTR_OP_SSH_PASSWORD", file=sys.stderr)
        raise SystemExit(2)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        host,
        username=USER,
        password=PW,
        timeout=30,
        banner_timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    return c


def run_script(c: paramiko.SSHClient, script: str, timeout: int = 180) -> tuple[int, str]:
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=timeout)
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


def upload_tree(sftp: paramiko.SFTPClient, local_dir: Path, remote_dir: str) -> None:
    ensure_dir(sftp, remote_dir)
    for p in local_dir.iterdir():
        if p.name in SKIP_DIRS or p.name.endswith(".db") or p.name.endswith(".pyc"):
            continue
        rp = posixpath.join(remote_dir, p.name)
        if p.is_dir():
            if p.name == "bgp_agent":
                continue
            upload_tree(sftp, p, rp)
        else:
            sftp.put(str(p), rp)

    bgp_dir = local_dir / "bgp_agent"
    if bgp_dir.is_dir():
        upload_bgp_agent_tree(sftp, bgp_dir, posixpath.join(remote_dir, "bgp_agent"))


def upload_bgp_agent_tree(sftp: paramiko.SFTPClient, local_dir: Path, remote_dir: str) -> None:
    """上传 Go 源码（跳过已编译二进制）。"""
    ensure_dir(sftp, remote_dir)
    for p in local_dir.rglob("*"):
        if p.is_dir():
            continue
        if p.name in {"bgp_agent", "bgp_agent.exe"} or p.suffix == ".exe":
            continue
        rel = p.relative_to(local_dir).as_posix()
        rp = posixpath.join(remote_dir, rel)
        parent = posixpath.dirname(rp)
        if parent:
            ensure_dir(sftp, parent)
        sftp.put(str(p), rp)


def main() -> None:
    service_dir = ROOT / "service"
    nfq = ROOT / "scripts" / "mtr_spoof_nfqueue.py"
    if not service_dir.is_dir():
        print("missing service/", file=sys.stderr)
        sys.exit(1)
    if not nfq.is_file():
        print("missing scripts/mtr_spoof_nfqueue.py", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting {USER}@{HOST} ...", flush=True)
    c = connect(HOST)
    sftp = c.open_sftp()
    try:
        upload_tree(sftp, service_dir, REMOTE)
        sftp.put(str(nfq), f"{REMOTE}/mtr_spoof_nfqueue.py")
    finally:
        sftp.close()

    restart = f"""
set -e
cd {REMOTE}
export GOBGP_AGENT_URL=http://127.0.0.1:9179
export MTR_OP_DB={REMOTE}/data.db
export MTR_OP_NFT={REMOTE}/nft_mtr_spoof.nft
export MTR_OP_DATA={REMOTE}/data
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
[ -f nft_mtr_spoof.nft ] && nft -f nft_mtr_spoof.nft || echo "WARN: nft load skipped"
: > /tmp/mtr_op.log
if [ -x ./venv/bin/uvicorn ]; then UV=./venv/bin/uvicorn; else UV='python3 -m uvicorn'; fi
nohup $UV app.main:app --host 0.0.0.0 --port 8808 >> /tmp/mtr_op.log 2>&1 &
sleep 5
for i in 1 2 3 4 5 6 7 8 9 10; do
  curl -sf http://127.0.0.1:8808/health >/dev/null && break
  sleep 1
done
: > /tmp/mtr_spoof_nfqueue.log
nohup $PY mtr_spoof_nfqueue.py --op-db {REMOTE}/data.db --verbose >> /tmp/mtr_spoof_nfqueue.log 2>&1 &
sleep 2
curl -sS http://127.0.0.1:8808/health; echo
pgrep -af 'uvicorn|mtr_spoof' || true
"""
    rebuild_bgp = os.environ.get("MTR_DEPLOY_BUILD_BGP_AGENT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    bgp_cfg = bgp_agent_config_from_env()
    bgp_cfg["remote_dir"] = REMOTE
    restart += shell_sync_bgp_agent(bgp_cfg, rebuild=rebuild_bgp)

    code, out = run_script(c, restart, timeout=300)
    print(out, end="")
    c.close()
    if code != 0:
        sys.exit(code)
    print("deploy_light_ok", flush=True)


if __name__ == "__main__":
    main()
