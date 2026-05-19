"""可选：同步 TE 映射到其它主机（仅 mtr 在该机跑且 TE 不经 200 转发时）。

默认关闭（不设置 MTR_TE_REWRITE_PEER_HOSTS）；逐跳改写只在 Linux 200 本机生效。
"""
from __future__ import annotations

import base64
import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_REMOTE_APPLY = r"""set -e
SCRIPT="${MTR_TE_REWRITE_SCRIPT:-/root/te_rewrite_nfqueue.py}"
QUEUE="${MTR_TE_QUEUE_NUM:-2}"
MAP="$(printf '%s' "$MAP_B64" | base64 -d 2>/dev/null || true)"
modprobe nfnetlink_queue 2>/dev/null || true
python3 -c 'import netfilterqueue, scapy' 2>/dev/null || {
  apt-get install -y -qq python3-scapy libnetfilter-queue1 2>/dev/null || true
}
if ! python3 -c 'import netfilterqueue' 2>/dev/null; then
  echo te_rewrite_peer: missing python3-netfilterqueue on peer >&2
  exit 2
fi
if ! test -f "$SCRIPT"; then
  echo te_rewrite_peer: missing script "$SCRIPT" >&2
  exit 2
fi
iptables -t mangle -D INPUT -p icmp -m icmp --icmp-type time-exceeded -j NFQUEUE --queue-num "$QUEUE" 2>/dev/null || true
iptables -t mangle -I INPUT 1 -p icmp -m icmp --icmp-type time-exceeded -j NFQUEUE --queue-num "$QUEUE"
pkill -f te_rewrite_nfqueue.py 2>/dev/null || true
export MTR_TE_REWRITE_MAP="$MAP"
python3 - <<'PY'
import os
m = os.environ.get("MTR_TE_REWRITE_MAP", "")
with open("/tmp/mtr_te_map.env", "w", encoding="ascii") as f:
    f.write("export MTR_TE_REWRITE_MAP=" + repr(m) + "\n")
PY
export MTR_TE_QUEUE_NUM="$QUEUE"
nohup python3 "$SCRIPT" >> /tmp/te_rewrite_nfqueue.log 2>&1 &
sleep 1
pgrep -af te_rewrite_nfqueue.py || exit 3
"""


def _peer_hosts() -> list[str]:
    raw = (os.environ.get("MTR_TE_REWRITE_PEER_HOSTS") or "").strip()
    if not raw:
        return []
    return [h.strip() for h in raw.split(",") if h.strip()]


def _ssh_password() -> str:
    return (
        (os.environ.get("MTR_TE_REWRITE_PEER_SSH_PASSWORD") or "").strip()
        or (os.environ.get("MTR_OP_SSH_PASSWORD") or "").strip()
    )


def _ssh_bin() -> str:
    return (os.environ.get("MTR_TE_REWRITE_PEER_SSH") or "").strip() or shutil.which("ssh") or "/usr/bin/ssh"


def _sshpass_bin() -> str:
    return (os.environ.get("MTR_TE_REWRITE_PEER_SSHPASS") or "").strip() or shutil.which("sshpass") or "/usr/bin/sshpass"


def _ssh_run(
    host: str,
    script: str,
    *,
    user: str,
    password: str,
    timeout: int,
) -> tuple[int, str]:
    target = f"{user}@{host}"
    ssh_opts = [
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
    ]
    env = os.environ.copy()
    if password:
        env["SSHPASS"] = password
        cmd = [_sshpass_bin(), "-e", _ssh_bin(), *ssh_opts, target, "bash", "-se"]
        try:
            r = subprocess.run(
                cmd,
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except FileNotFoundError:
            logger.debug("sshpass not found, try paramiko for %s", host)
        except subprocess.TimeoutExpired:
            return -1, "ssh timeout"

    try:
        import paramiko  # type: ignore
    except ImportError:
        if password:
            return -1, "sshpass and paramiko unavailable for peer sync"
        return -1, "no SSH password and paramiko unavailable"

    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            host,
            username=user,
            password=password or None,
            timeout=15,
            allow_agent=not password,
            look_for_keys=not password,
        )
        _, stdout, stderr = c.exec_command("bash -se", timeout=timeout)
        stdin = stdout.channel.makefile("wb", -1)
        stdin.write(script.encode())
        stdin.close()
        out = stdout.read().decode("utf-8", errors="replace") + stderr.read().decode(
            "utf-8", errors="replace"
        )
        code = stdout.channel.recv_exit_status()
        c.close()
        return code, out
    except Exception as e:
        return -1, str(e)


def _scp_script(host: str, local: Path, remote: str, *, user: str, password: str) -> bool:
    if not local.is_file():
        return False
    target = f"{user}@{host}:{remote}"
    ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
    env = os.environ.copy()
    if password:
        env["SSHPASS"] = password
        try:
            r = subprocess.run(
                [_sshpass_bin(), "-e", shutil.which("scp") or "/usr/bin/scp", *ssh_opts, str(local), target],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
            if r.returncode == 0:
                return True
        except FileNotFoundError:
            pass
    try:
        import paramiko  # type: ignore

        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            host,
            username=user,
            password=password or None,
            timeout=15,
            allow_agent=not password,
            look_for_keys=not password,
        )
        sftp = c.open_sftp()
        sftp.put(str(local), remote)
        sftp.close()
        c.close()
        return True
    except Exception as e:
        logger.warning("te_rewrite_peer scp %s: %s", host, e)
        return False


def sync_te_rewrite_peers(map_line: str, *, local_script: Path | None = None) -> None:
    if os.environ.get("MTR_TE_REWRITE_PEER_SKIP", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return
    hosts = _peer_hosts()
    if not hosts:
        return
    user = (os.environ.get("MTR_TE_REWRITE_PEER_SSH_USER") or "root").strip() or "root"
    password = _ssh_password()
    if not password:
        logger.warning(
            "te_rewrite_peer_sync: MTR_TE_REWRITE_PEER_HOSTS set but no "
            "MTR_OP_SSH_PASSWORD / MTR_TE_REWRITE_PEER_SSH_PASSWORD"
        )
        return
    remote_script = (
        os.environ.get("MTR_TE_REWRITE_PEER_SCRIPT") or "/root/te_rewrite_nfqueue.py"
    ).strip()
    queue = (os.environ.get("MTR_TE_REWRITE_PEER_QUEUE") or "2").strip() or "2"
    map_b64 = base64.b64encode(map_line.encode("utf-8")).decode("ascii")
    timeout = int(os.environ.get("MTR_TE_REWRITE_PEER_SSH_TIMEOUT", "45"))
    src = local_script
    if src is None:
        root = Path(__file__).resolve().parent.parent
        src = root / "te_rewrite_nfqueue.py"
        if not src.is_file():
            alt = root.parent / "scripts" / "te_rewrite_nfqueue.py"
            if alt.is_file():
                src = alt

    preamble = (
        f"export MTR_TE_REWRITE_SCRIPT={shlex.quote(remote_script)}\n"
        f"export MTR_TE_QUEUE_NUM={shlex.quote(queue)}\n"
        f"export MAP_B64={shlex.quote(map_b64)}\n"
    )
    body = preamble + _REMOTE_APPLY

    for host in hosts:
        check = f"test -f {shlex.quote(remote_script)}"
        code, _ = _ssh_run(host, check, user=user, password=password, timeout=15)
        if code != 0 and src and src.is_file():
            _scp_script(host, src, remote_script, user=user, password=password)
        code, out = _ssh_run(host, body, user=user, password=password, timeout=timeout)
        if code != 0:
            logger.warning(
                "te_rewrite_peer_sync: %s failed code=%s: %s",
                host,
                code,
                out.strip()[:800],
            )
        else:
            logger.info(
                "te_rewrite_peer_sync: %s ok map_len=%s",
                host,
                len(map_line),
            )
