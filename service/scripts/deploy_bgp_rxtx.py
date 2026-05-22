#!/usr/bin/env python3
"""部署 BGP RX/TX 新架构到远程 VR（含 GoBGP Agent + Python OP）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

OP_HOST = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
OP_USER = os.environ.get("MTR_OP_SSH_USER", "root").strip()
OP_PASS = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
OP_DIR = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").strip()
OP_PORT = int(os.environ.get("MTR_OP_PORT", "8808"))
SKIP_INSTALL = os.environ.get("MTR_OP_SKIP_INSTALL", "").strip().lower() in {"1", "true", "yes"}
PRESERVE_DIR = os.environ.get("MTR_OP_PRESERVE_DIR", "").strip().lower() in {"1", "true", "yes"} or SKIP_INSTALL

RR_ADDR = os.environ.get("RR_ADDR", "139.159.43.249")
RR_AS = os.environ.get("RR_AS", "63199")
LOCAL_AS = os.environ.get("LOCAL_AS", "63199")
ROUTER_ID = os.environ.get("ROUTER_ID", "139.159.43.207")
REDIS_ADDR = os.environ.get("REDIS_ADDR", "localhost:6379")
ROCKSDB_PATH = os.environ.get("ROCKSDB_PATH", "/var/lib/bgp_agent/rocksdb")
API_ADDR = os.environ.get("API_ADDR", ":9179")

OP_DOWNSTREAM_IFACE = os.environ.get("MTR_OP_DOWNSTREAM_IFACE", "eno1np0")
OP_RR_UPLINK_IFACE = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0")
TE_REWRITE_OIF = os.environ.get("MTR_TE_REWRITE_OIF", OP_DOWNSTREAM_IFACE).strip()
TE_REWRITE_IIF = os.environ.get("MTR_TE_REWRITE_IIF", OP_RR_UPLINK_IFACE).strip()
RR_SPOOF_IPVLAN_ADDR = os.environ.get("MTR_BGP_RR_SPOOF_IPVLAN_ADDR", "0").strip()
SATELLITE_PEER_IP = os.environ.get("MTR_SATELLITE_PEER_IP", "139.159.43.208")
PROBE_SSH_HOST = os.environ.get("MTR_PROBE_SSH_HOST", OP_HOST)

SKIP_NAMES = {"venv", ".venv", "__pycache__", ".git", ".idea"}
SKIP_FILE_SUFFIX = (".db", ".pyc", ".exe")
SKIP_FILE_NAMES = {"bgp_agent", "go.tar.gz"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _tools_path() -> str:
    return str(_repo_root() / "tools")


def bash(c: paramiko.SSHClient, script: str, timeout: int = 1200) -> tuple[int, str]:
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=timeout)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), out + err


def upload_tree(sftp: paramiko.SFTPClient, local_dir: str, remote_dir: str) -> None:
    for name in os.listdir(local_dir):
        if name in SKIP_NAMES or name.endswith(".db"):
            continue
        lp = os.path.join(local_dir, name)
        rp = f"{remote_dir}/{name}".replace("\\", "/")
        if os.path.isdir(lp):
            try:
                sftp.mkdir(rp)
            except OSError:
                pass
            upload_tree(sftp, lp, rp)
        elif name in SKIP_FILE_NAMES:
            continue
        elif name.endswith(SKIP_FILE_SUFFIX):
            continue
        elif not name.endswith(".pyc"):
            sftp.put(lp, rp)


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() and k.strip() not in os.environ:
            os.environ[k.strip()] = v.strip()


def main() -> None:
    root = _repo_root()
    _load_env_file(root / "109" / "env")
    op_pass = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not op_pass:
        print("请设置环境变量 MTR_OP_SSH_PASSWORD（或 109/env）", file=sys.stderr)
        sys.exit(2)
    op_host = os.environ.get("MTR_OP_HOST", OP_HOST).strip()
    op_user = os.environ.get("MTR_OP_SSH_USER", OP_USER).strip()
    op_dir = os.environ.get("MTR_OP_REMOTE_DIR", OP_DIR).strip()
    op_port = int(os.environ.get("MTR_OP_PORT", str(OP_PORT)))
    downstream = os.environ.get("MTR_OP_DOWNSTREAM_IFACE", OP_DOWNSTREAM_IFACE).strip()
    rr_uplink = os.environ.get("MTR_BGP_RR_UPLINK_IFACE", OP_RR_UPLINK_IFACE).strip()
    te_oif = os.environ.get("MTR_TE_REWRITE_OIF", downstream).strip()
    te_iif = os.environ.get("MTR_TE_REWRITE_IIF", rr_uplink).strip()
    rr_spoof = os.environ.get("MTR_BGP_RR_SPOOF_IPVLAN_ADDR", RR_SPOOF_IPVLAN_ADDR).strip()
    sat_peer = os.environ.get("MTR_SATELLITE_PEER_IP", SATELLITE_PEER_IP).strip()
    probe_host = os.environ.get("MTR_PROBE_SSH_HOST", op_host).strip()
    service = str(root / "service")
    sys.path.insert(0, _tools_path())
    from bgp_agent_build import (  # noqa: E402
        bgp_agent_binary,
        build_bgp_agent_for_deploy,
        prebuilt_deploy_enabled,
        remote_rebuild_enabled,
        should_run_local_build,
    )

    use_prebuilt = prebuilt_deploy_enabled(root)
    if use_prebuilt and should_run_local_build(root):
        print("\n=== 本地编译 bgp_agent（跳过 VR go build）===", flush=True)
        build_bgp_agent_for_deploy(root)
    elif use_prebuilt and not bgp_agent_binary(root).is_file():
        print("未找到 service/bgp_agent/bgp_agent，请先: python tools/bgp_agent_build.py", file=sys.stderr)
        sys.exit(2)
    te_nfq = str(root / "scripts" / "te_rewrite_nfqueue.py")
    if not os.path.isdir(service):
        print(f"缺少 service/: {service}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(te_nfq):
        print(f"缺少 {te_nfq}", file=sys.stderr)
        sys.exit(1)

    print("=" * 60)
    print("BGP RX/TX 架构部署")
    print(f"  目标: {op_user}@{op_host}:{op_dir}")
    print(f"  RR:   {RR_ADDR} AS{RR_AS}")
    print(f"  本地: AS{LOCAL_AS} router-id {ROUTER_ID}")
    print("=" * 60)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        op_host,
        username=op_user,
        password=op_pass,
        timeout=30,
        banner_timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )

    step1 = "保留远端目录（不删 data.db）" if PRESERVE_DIR else "停止旧服务并清理远端目录"
    print(f"\n=== 1. {step1} ===")
    if PRESERVE_DIR:
        prep = f"""
set -e
pkill -f 'uvicorn app.main' 2>/dev/null || true
pkill -f mtr_spoof_nfqueue 2>/dev/null || true
sleep 1
mkdir -p {op_dir}
mkdir -p $(dirname {ROCKSDB_PATH})
echo "保留目录完成"
"""
    else:
        prep = f"""
set -e
systemctl stop bgp-agent 2>/dev/null || true
systemctl disable bgp-agent 2>/dev/null || true
pkill -f bgp_agent 2>/dev/null || true
pkill -f 'uvicorn app.main' 2>/dev/null || true
pkill -f mtr_spoof_nfqueue 2>/dev/null || true
sleep 2
rm -rf {op_dir}
mkdir -p {op_dir}
mkdir -p $(dirname {ROCKSDB_PATH})
echo "清理完成"
"""
    code, out = bash(c, prep)
    print(out)
    if code != 0:
        print(f"WARN: 清理退出码 {code}")

    print("\n=== 2. 上传代码 ===")
    sftp = c.open_sftp()
    try:
        upload_tree(sftp, service, op_dir)
        sftp.put(te_nfq, f"{op_dir}/te_rewrite_nfqueue.py")
        if use_prebuilt:
            local_bin = bgp_agent_binary(root)
            remote_bin = f"{op_dir}/bgp_agent/bgp_agent"
            try:
                sftp.mkdir(f"{op_dir}/bgp_agent")
            except OSError:
                pass
            sftp.put(str(local_bin), remote_bin)
            print(f"  预编译 bgp_agent -> {remote_bin} ({local_bin.stat().st_size} bytes)")
    finally:
        sftp.close()
    print("上传完成")

    if not SKIP_INSTALL:
        print("\n=== 3. 安装系统依赖 (Go / Redis / RocksDB / Python) ===")
        install = f"""
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \\
  redis-server librocksdb-dev g++ build-essential pkg-config \\
  python3-venv python3-pip nftables python3-scapy python3-dev \\
  libnetfilter-queue-dev iptables iproute2 mtr traceroute \\
  ca-certificates curl wget

systemctl enable redis-server
systemctl start redis-server
redis-cli ping || (echo "Redis failed" && exit 1)

if ! command -v go >/dev/null 2>&1 || ! go version | grep -qE 'go1\\.(2[1-9]|[3-9])'; then
  echo "安装 Go 1.21..."
  export GOPROXY=https://goproxy.cn,direct
  export GOSUMDB=sum.golang.google.cn
  cd /tmp
  curl -fsSL -o go.tar.gz https://go.dev/dl/go1.21.13.linux-amd64.tar.gz
  rm -rf /usr/local/go
  tar -C /usr/local -xzf go.tar.gz
  rm -f go.tar.gz
fi
export PATH=/usr/local/go/bin:$PATH
go version

cd {op_dir}
rm -rf venv
python3 -m venv venv
./venv/bin/pip install -U pip wheel setuptools -q \\
  -i https://pypi.tuna.tsinghua.edu.cn/simple
./venv/bin/pip install -r requirements.txt -q \\
  -i https://pypi.tuna.tsinghua.edu.cn/simple
./venv/bin/pip install NetfilterQueue scapy paramiko -q \\
  -i https://pypi.tuna.tsinghua.edu.cn/simple || true
echo "Python 依赖 OK"
"""
        code, out = bash(c, install, timeout=1200)
        print(out)
        if code != 0:
            print(f"安装失败 exit={code}", file=sys.stderr)
            sys.exit(code)

    remote_build = remote_rebuild_enabled() and not use_prebuilt
    print(
        "\n=== 4. 安装 BGP Agent ==="
        + (" (VR go build)" if remote_build else " (本机预编译二进制)")
    )
    build_preamble = f"""
set -e
mkdir -p {ROCKSDB_PATH}
chown -R root:root $(dirname {ROCKSDB_PATH}) || true
"""
    compile_block = ""
    if remote_build:
        compile_block = f"""
export PATH=/usr/local/go/bin:$PATH
export GOPROXY=https://goproxy.cn,direct
export GOSUMDB=sum.golang.google.cn
export CGO_ENABLED=1
cd {op_dir}/bgp_agent
go mod tidy
go mod download
go build -o bgp_agent -ldflags="-s -w" .
test -x ./bgp_agent
echo "远端编译成功: $(./bgp_agent -h 2>&1 | head -1 || true)"
"""
    else:
        compile_block = f"""
chmod +x {op_dir}/bgp_agent/bgp_agent
test -x {op_dir}/bgp_agent/bgp_agent || {{ echo "缺少预编译 {op_dir}/bgp_agent/bgp_agent"; exit 2; }}
echo "使用本机预编译: $(ls -la {op_dir}/bgp_agent/bgp_agent)"
"""
    build = (
        build_preamble
        + compile_block
        + f"""
cat > /etc/systemd/system/bgp-agent.service <<'UNIT'
[Unit]
Description=BGP RX/TX Agent (GoBGP)
After=network.target redis-server.service
Wants=redis-server.service

[Service]
Type=simple
WorkingDirectory={op_dir}/bgp_agent
Environment=PATH=/usr/local/go/bin:/usr/bin:/bin
ExecStart={op_dir}/bgp_agent/bgp_agent \\
  -local-as {LOCAL_AS} -router-id {ROUTER_ID} \\
  -redis {REDIS_ADDR} -rocksdb {ROCKSDB_PATH} \\
  -api {API_ADDR}
Restart=always
RestartSec=10
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable bgp-agent
systemctl restart bgp-agent
sleep 3
systemctl is-active bgp-agent
curl -sf http://127.0.0.1:9179/health && echo " BGP Agent health OK" || echo " WARN: health check failed"
curl -sf http://127.0.0.1:9179/api/status | head -c 500 || true
echo ""
"""
    code, out = bash(c, build, timeout=900)
    print(out)
    if code != 0:
        print(f"BGP Agent 部署失败 exit={code}", file=sys.stderr)
        _, o, _ = c.exec_command("journalctl -u bgp-agent -n 40 --no-pager")
        print(o.read().decode())
        sys.exit(code)

    print("\n=== 5. 启动 Python OP + NFQUEUE ===")
    run = f"""
set -e
if [ ! -x {op_dir}/venv/bin/uvicorn ]; then
  echo "创建 Python venv..."
  cd {op_dir}
  python3 -m venv venv
  ./venv/bin/pip install -U pip wheel setuptools -q -i https://pypi.tuna.tsinghua.edu.cn/simple
  ./venv/bin/pip install -r requirements.txt -q -i https://pypi.tuna.tsinghua.edu.cn/simple
fi
pkill -f 'uvicorn app.main' 2>/dev/null || true
pkill -f mtr_spoof_nfqueue 2>/dev/null || true
sleep 1
nft delete table inet mtr_spoof 2>/dev/null || true
nft delete table inet mtr_te 2>/dev/null || true
if [ -f {op_dir}/nft_mtr_te.nft ]; then
  nft -f {op_dir}/nft_mtr_te.nft || echo "WARN: nft load failed"
fi

cd {op_dir}
export GOBGP_AGENT_URL=http://127.0.0.1:9179
export MTR_OP_DB={op_dir}/data.db
export MTR_OP_NFT={op_dir}/nft_mtr_te.nft
export MTR_TE_REWRITE_SCRIPT={op_dir}/te_rewrite_nfqueue.py
export MTR_OP_DATA={op_dir}/data
export MTR_OP_DOWNSTREAM_IFACE={downstream}
export MTR_BGP_IPVLAN_AUTO=1
export MTR_BGP_IPVLAN_BASE_IFACE={downstream}
export MTR_BGP_RR_UPLINK_IFACE={rr_uplink}
export MTR_TE_REWRITE_OIF={te_oif}
export MTR_TE_REWRITE_IIF={te_iif}
export MTR_BGP_RR_SPOOF_IPVLAN_ADDR={rr_spoof}
export MTR_BGP_IPVLAN_PEER_IP={sat_peer}
export MTR_PROBE_SSH_HOST={probe_host}

: > /tmp/mtr_op.log
nohup env MTR_OP_DB="$MTR_OP_DB" MTR_OP_NFT="$MTR_OP_NFT" \\
  MTR_TE_REWRITE_SCRIPT="$MTR_TE_REWRITE_SCRIPT" \\
  MTR_OP_DOWNSTREAM_IFACE="$MTR_OP_DOWNSTREAM_IFACE" \\
  MTR_TE_REWRITE_OIF="$MTR_TE_REWRITE_OIF" MTR_TE_REWRITE_IIF="$MTR_TE_REWRITE_IIF" \\
  MTR_BGP_IPVLAN_BASE_IFACE="$MTR_BGP_IPVLAN_BASE_IFACE" \\
  MTR_BGP_RR_UPLINK_IFACE="$MTR_BGP_RR_UPLINK_IFACE" \\
  ./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port {op_port} >> /tmp/mtr_op.log 2>&1 &
sleep 4

echo "=== 进程 ==="
pgrep -af bgp_agent || true
pgrep -af uvicorn || true
pgrep -af te_rewrite || true

echo "=== 健康检查 ==="
curl -s http://127.0.0.1:{op_port}/health || true
echo ""
curl -s http://127.0.0.1:{op_port}/api/gobgp/status || true
echo ""
"""
    code, out = bash(c, run, timeout=180)
    print(out)
    c.close()

    print("\n" + "=" * 60)
    print("部署完成")
    print(f"  管理界面: http://{op_host}:{op_port}/")
    print(f"  GoBGP API: http://{OP_HOST}:9179/api/status")
    print("=" * 60)


if __name__ == "__main__":
    main()
