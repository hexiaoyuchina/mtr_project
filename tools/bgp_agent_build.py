#!/usr/bin/env python3
"""编译 Linux amd64 的 bgp_agent，供 deploy_light 上传远端。

- 默认：本机（Linux）或 WSL
- --remote：在 VR 上 go build 后拉回本机（无 WSL/Go 时用）
"""
from __future__ import annotations

import argparse
import os
import platform
import posixpath
import subprocess
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    paramiko = None  # type: ignore


def bgp_agent_dir(root: Path) -> Path:
    return root / "service" / "bgp_agent"


def bgp_agent_binary(root: Path) -> Path:
    return bgp_agent_dir(root) / "bgp_agent"


def _windows_path_for_wsl(path: Path) -> str:
    """wslpath -a 需用正斜杠，避免 D:\\mtr 中 \\m 被吞。"""
    return path.resolve().as_posix()


_APT_ROCKSDB = (
    "librocksdb-dev libsnappy-dev zlib1g-dev libbz2-dev liblz4-dev libzstd-dev "
    "gcc g++ pkg-config"
)


def _wsl_path_for_file(wsl_args: list[str], path: Path) -> str:
    """Windows 文件 -> WSL 路径（供 bash 脚本引用）。"""
    if not path.is_file():
        return ""
    r = subprocess.run(
        ["wsl", *wsl_args, "wslpath", "-a", _windows_path_for_wsl(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _wsl_prepare_script(_rocksdb_tar_posix: str = "") -> str:
    """WSL 内确保 Go>=1.21 与 RocksDB 6.11 开发库（gorocksdb 不兼容 8.x）。"""
    return f"""
set -e
export PATH=/usr/local/go/bin:/usr/sbin:/usr/bin:/sbin:/bin
if [ "$(id -u)" -eq 0 ]; then SUDO=; else SUDO=sudo; fi
export DEBIAN_FRONTEND=noninteractive
if ! command -v go >/dev/null 2>&1 && [ ! -x /usr/local/go/bin/go ]; then
  echo "=== WSL: 安装 Go ==="
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq golang-go curl ca-certificates
fi
if ! command -v go >/dev/null 2>&1 && [ ! -x /usr/local/go/bin/go ]; then
  GO_TGZ=go1.21.13.linux-amd64.tar.gz
  mkdir -p /usr/local/go/bin
  for url in \\
    "https://mirrors.aliyun.com/golang/$GO_TGZ" \\
    "https://golang.google.cn/dl/$GO_TGZ" \\
    "https://go.dev/dl/$GO_TGZ"; do
    curl -fsSL --connect-timeout 30 "$url" -o "/tmp/$GO_TGZ" && break
  done
  test -s "/tmp/$GO_TGZ"
  $SUDO rm -rf /usr/local/go
  $SUDO tar -C /usr/local -xzf "/tmp/$GO_TGZ"
  rm -f "/tmp/$GO_TGZ"
fi
_rocksdb_needs_v6() {{
  if [ ! -f /usr/include/rocksdb/version.h ]; then return 0; fi
  grep -qE '#define ROCKSDB_MAJOR[[:space:]]+([7-9]|[1-9][0-9]+)' /usr/include/rocksdb/version.h 2>/dev/null
}}

if _rocksdb_needs_v6; then
  echo "=== WSL: 安装 RocksDB 6.11（gorocksdb 与 Ubuntu 24.04 自带 8.x 不兼容）==="
  $SUDO rm -rf /usr/local/include/rocksdb /usr/local/lib/librocksdb*
  $SUDO apt-get remove -y librocksdb-dev librocksdb8.9 2>/dev/null || true
  if [ ! -f /etc/apt/sources.list.d/jammy-rocksdb.list ]; then
    echo "deb [arch=amd64] http://archive.ubuntu.com/ubuntu jammy universe" > /etc/apt/sources.list.d/jammy-rocksdb.list
    $SUDO apt-get update -qq
  fi
  $SUDO apt-get install -y -qq -o APT::Get::Allow-Downgrades=true librocksdb-dev=6.11.4-3 librocksdb6.11 {_APT_ROCKSDB}
elif ! pkg-config --exists rocksdb 2>/dev/null && ! [ -f /usr/include/rocksdb/db.h ]; then
  echo "=== WSL: 安装 librocksdb-dev ==="
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq {_APT_ROCKSDB}
fi
export PATH=/usr/local/go/bin:/usr/bin:/usr/sbin:/sbin:/bin
export CGO_CFLAGS=""
export CGO_LDFLAGS="-lrocksdb"
go version
"""


def _build_script(
    agent_dir_posix: str, goos: str, goarch: str, rocksdb_tar_posix: str = ""
) -> str:
    return (
        _wsl_prepare_script(rocksdb_tar_posix)
        + f"""
export CGO_ENABLED=1
export GOOS={goos}
export GOARCH={goarch}
export GOPROXY=${{GOPROXY:-https://goproxy.cn,direct}}
export GOSUMDB=${{GOSUMDB:-sum.golang.google.cn}}
export PATH=/usr/local/go/bin:/usr/bin:/usr/sbin:/sbin:/bin
cd {agent_dir_posix}
go mod tidy
go mod download
go build -o bgp_agent -ldflags="-s -w" .
test -x ./bgp_agent
file ./bgp_agent || true
echo "local_build_ok: $(pwd)/bgp_agent"
"""
    )


def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            check=False,
        )
        return r.returncode == 0
    except FileNotFoundError:
        return False


def build_docker_bgp_agent(root: Path) -> Path:
    """用 Docker 编译 linux/amd64（无 WSL 时可用）。"""
    agent_dir = bgp_agent_dir(root)
    dockerfile = root / "tools" / "docker" / "bgp_agent_build.Dockerfile"
    if not dockerfile.is_file():
        raise FileNotFoundError(dockerfile)
    goos = os.environ.get("MTR_BGP_AGENT_GOOS", "linux").strip() or "linux"
    goarch = os.environ.get("MTR_BGP_AGENT_GOARCH", "amd64").strip() or "amd64"
    out = bgp_agent_binary(root)
    tag = "mtr-bgp-agent-build:local"
    print(f"=== Docker 编译 bgp_agent ({goos}/{goarch}) ===", flush=True)
    build_cmd = [
        "docker",
        "build",
        "-f",
        str(dockerfile),
        "--build-arg",
        f"GOOS={goos}",
        "--build-arg",
        f"GOARCH={goarch}",
        "-t",
        tag,
        str(agent_dir),
    ]
    r = subprocess.run(build_cmd, cwd=str(root))
    if r.returncode != 0:
        raise SystemExit(r.returncode)
    out_dir = root / ".build" / "bgp_agent"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{out_dir.resolve()}:/host",
        tag,
        "sh",
        "-c",
        "cp /out/bgp_agent /host/bgp_agent && chmod +x /host/bgp_agent",
    ]
    r = subprocess.run(run_cmd, cwd=str(root))
    if r.returncode != 0:
        raise SystemExit(r.returncode)
    built = out_dir / "bgp_agent"
    if not built.is_file():
        print(f"Docker 编译产物不存在: {built}", file=sys.stderr)
        raise SystemExit(1)
    built.replace(out)
    print(f"=== Docker 编译完成 {out} ({out.stat().st_size} bytes) ===", flush=True)
    return out


def _wsl_cli_prefix() -> list[str]:
    """wsl 命令前缀：-d distro、-u user（可选）。"""
    args: list[str] = []
    distro = os.environ.get("MTR_BGP_AGENT_WSL_DISTRO", "").strip()
    if distro:
        args.extend(["-d", distro])
    user = os.environ.get("MTR_BGP_AGENT_WSL_USER", "").strip()
    if user:
        args.extend(["-u", user])
    return args


def _wsl_distro() -> list[str]:
    """返回 wsl 命令前缀（-d / -u），无则 []。"""
    preset = _wsl_cli_prefix()
    if preset:
        return preset
    r = subprocess.run(
        ["wsl", "-l", "-q"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return []
    for line in r.stdout.splitlines():
        name = line.strip().strip("\x00")
        if name and "docker" not in name.lower():
            return ["-d", name.split()[0]]
    return []


def build_local_bgp_agent(root: Path) -> Path:
    """编译 service/bgp_agent/bgp_agent（目标 GOOS/GOARCH，默认 linux/amd64）。"""
    agent_dir = bgp_agent_dir(root)
    if not (agent_dir / "go.mod").is_file():
        raise FileNotFoundError(f"missing go.mod: {agent_dir}")

    goos = os.environ.get("MTR_BGP_AGENT_GOOS", "linux").strip() or "linux"
    goarch = os.environ.get("MTR_BGP_AGENT_GOARCH", "amd64").strip() or "amd64"
    out = bgp_agent_binary(root)

    if platform.system() == "Windows":
        wsl_args = _wsl_distro()
        wsl = subprocess.run(
            ["wsl", *wsl_args, "wslpath", "-a", _windows_path_for_wsl(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        if wsl.returncode != 0:
            if _docker_available():
                return build_docker_bgp_agent(root)
            print(
                "Windows 本地编译需要 WSL 或 Docker。\n"
                "  WSL: 安装后若提示重启，请重启再执行 python tools/bgp_agent_build.py\n"
                "  Docker: 安装 Docker Desktop 后重试\n"
                "  远端: set MTR_BGP_AGENT_BUILD_REMOTE=1",
                file=sys.stderr,
            )
            raise SystemExit(2)
        wsl_root = wsl.stdout.strip()
        agent_posix = f"{wsl_root}/service/bgp_agent"
        rocks_tar = _wsl_path_for_file(
            wsl_args, root / ".build" / "rocksdb-6.29.5.tar.gz"
        )
        cmd = [
            "wsl",
            *wsl_args,
            "bash",
            "-lc",
            _build_script(agent_posix, goos, goarch, rocks_tar),
        ]
    else:
        cmd = ["bash", "-lc", _build_script(str(agent_dir.resolve()), goos, goarch)]

    print(f"=== 本地编译 bgp_agent ({goos}/{goarch}) ===", flush=True)
    r = subprocess.run(cmd, cwd=str(root))
    if r.returncode != 0:
        raise SystemExit(r.returncode)
    if not out.is_file():
        print(f"编译产物不存在: {out}", file=sys.stderr)
        raise SystemExit(1)
    return out


def remote_rebuild_enabled() -> bool:
    """是否在 VR 上 go build（默认关，改用本地预编译上传）。"""
    return os.environ.get("MTR_DEPLOY_BUILD_BGP_AGENT", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def prebuilt_deploy_enabled(root: Path) -> bool:
    """是否上传预编译二进制并跳过远端 go build（默认开）。"""
    mode = os.environ.get("MTR_DEPLOY_BGP_AGENT_PREBUILT", "1").strip().lower()
    if mode in {"0", "false", "no"}:
        return False
    if mode in {"1", "true", "yes"}:
        return True
    # auto：已有 linux 二进制则上传
    if mode == "auto":
        bin_path = bgp_agent_binary(root)
        return bin_path.is_file() and bin_path.stat().st_size > 0
    return True


def _upload_bgp_sources(sftp: paramiko.SFTPClient, local_dir: Path, remote_dir: str) -> None:
    for p in local_dir.rglob("*"):
        if p.is_dir():
            continue
        if p.name in {"bgp_agent", "bgp_agent.exe"} or p.suffix == ".exe":
            continue
        rel = p.relative_to(local_dir).as_posix()
        rp = posixpath.join(remote_dir, rel)
        parent = posixpath.dirname(rp)
        parts = parent.strip("/").split("/")
        cur = ""
        for part in parts:
            cur += "/" + part
            try:
                sftp.mkdir(cur)
            except OSError:
                pass
        sftp.put(str(p), rp)


def build_remote_bgp_agent(root: Path) -> Path:
    """在现网 VR 编译并拉回 service/bgp_agent/bgp_agent。"""
    if paramiko is None:
        print("pip install paramiko", file=sys.stderr)
        raise SystemExit(2)

    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    user = os.environ.get("MTR_OP_SSH_USER", "root").strip()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    remote_dir = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").strip()
    if not pw:
        print("请设置 MTR_OP_SSH_PASSWORD（或 109/env）", file=sys.stderr)
        raise SystemExit(2)

    agent_dir = bgp_agent_dir(root)
    remote_agent = posixpath.join(remote_dir, "bgp_agent")
    build_sh = f"""
set -e
export PATH=/usr/local/go/bin:$PATH
export GOPROXY=${{GOPROXY:-https://goproxy.cn,direct}}
export GOSUMDB=${{GOSUMDB:-sum.golang.google.cn}}
export CGO_ENABLED=1
mkdir -p {remote_agent}
cd {remote_agent}
if ! command -v go >/dev/null 2>&1; then
  echo "远端未安装 Go，请先跑全量部署或安装 /usr/local/go"
  exit 2
fi
go mod tidy
go mod download
go build -o bgp_agent -ldflags="-s -w" .
test -x ./bgp_agent
ls -la ./bgp_agent
echo remote_build_ok
"""

    print(f"=== 远端编译 bgp_agent @ {user}@{host}:{remote_agent} ===", flush=True)
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
    sftp = c.open_sftp()
    try:
        _upload_bgp_sources(sftp, agent_dir, remote_agent)
    finally:
        sftp.close()

    stdin, stdout, stderr = c.exec_command("bash -se", timeout=2400)
    stdin.write(build_sh)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    print(out, end="")
    if err:
        print(err, end="", file=sys.stderr)
    if code != 0:
        c.close()
        raise SystemExit(code)

    out_local = bgp_agent_binary(root)
    sftp = c.open_sftp()
    try:
        sftp.get(posixpath.join(remote_agent, "bgp_agent"), str(out_local))
    finally:
        sftp.close()
    c.close()
    print(f"=== 已拉回 {out_local} ({out_local.stat().st_size} bytes) ===", flush=True)
    return out_local


def should_run_local_build(root: Path) -> bool:
    """部署前是否在本机/WSL/Docker 编译 bgp_agent。"""
    mode = os.environ.get("MTR_DEPLOY_BGP_AGENT_LOCAL_BUILD", "auto").strip().lower()
    if mode in {"0", "false", "no"}:
        return False
    if mode in {"1", "true", "yes"}:
        return prebuilt_deploy_enabled(root)
    # auto（默认）：无 linux 二进制时编译
    if not prebuilt_deploy_enabled(root):
        return False
    bin_path = bgp_agent_binary(root)
    return not (bin_path.is_file() and bin_path.stat().st_size > 0)


def _remote_build_enabled() -> bool:
    mode = os.environ.get("MTR_BGP_AGENT_BUILD_REMOTE", "").strip().lower()
    if mode in {"0", "false", "no"}:
        return False
    return mode in {"1", "true", "yes"}


def _wsl_available(root: Path) -> bool:
    if platform.system() != "Windows":
        return True
    wsl_args = _wsl_distro()
    wsl = subprocess.run(
        ["wsl", *wsl_args, "wslpath", "-a", _windows_path_for_wsl(root)],
        capture_output=True,
        check=False,
    )
    return wsl.returncode == 0


def build_bgp_agent_for_deploy(root: Path) -> Path:
    """优先 WSL → Docker；MTR_BGP_AGENT_BUILD_REMOTE=1 时在 VR 编译。"""
    if _remote_build_enabled():
        return build_remote_bgp_agent(root)
    if platform.system() == "Windows":
        if _wsl_available(root):
            return build_local_bgp_agent(root)
        if _docker_available():
            return build_docker_bgp_agent(root)
        print(
            "本机无法编译 bgp_agent（需 WSL 或 Docker）。\n"
            "  · 刚安装 WSL/Ubuntu：请重启 Windows 后执行 python tools/bgp_agent_build.py\n"
            "  · 或安装 Docker Desktop\n"
            "  · 或 set MTR_BGP_AGENT_BUILD_REMOTE=1 在 109 远端编译\n",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return build_local_bgp_agent(root)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", action="store_true", help="在 VR 上编译并拉回本机")
    ap.add_argument("--docker", action="store_true", help="用 Docker 编译（跳过 WSL）")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    env_file = root / "109" / "env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()
    if args.remote:
        build_remote_bgp_agent(root)
    elif args.docker:
        build_docker_bgp_agent(root)
    else:
        build_bgp_agent_for_deploy(root)


if __name__ == "__main__":
    main()
