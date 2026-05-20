#!/usr/bin/env python3
"""仅部署 OUTPUT NFQUEUE 逐跳改动（te_rewrite_sync.py），并远程触发 sync + 验收。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

DIR = Path(__file__).resolve().parent
ROOT = DIR.parent
REMOTE = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
        break


def connect() -> paramiko.SSHClient:
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    user = os.environ.get("MTR_OP_SSH_USER", "root").strip()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not pw:
        print("请配置 109/env 中 MTR_OP_SSH_PASSWORD", file=sys.stderr)
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


def run(c: paramiko.SSHClient, script: str, timeout: int = 120) -> tuple[int, str]:
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=timeout)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), out


def main() -> None:
    load_env()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    local_sync = ROOT / "service" / "app" / "te_rewrite_sync.py"
    local_nfq = ROOT / "scripts" / "te_rewrite_nfqueue.py"
    if not local_sync.is_file():
        print(f"missing {local_sync}", file=sys.stderr)
        raise SystemExit(1)

    down = os.environ.get("MTR_TE_REWRITE_OIF") or os.environ.get("MTR_OP_DOWNSTREAM_IFACE", "eno1np0")
    up = os.environ.get("MTR_TE_REWRITE_IIF") or os.environ.get("MTR_BGP_RR_UPLINK_IFACE", "enp59s0f0np0")
    te_out = os.environ.get("MTR_TE_REWRITE_OUTPUT", "1")

    print(f"Connecting {host} ...", flush=True)
    c = connect()
    sftp = c.open_sftp()
    try:
        sftp.put(str(local_sync), f"{REMOTE}/app/te_rewrite_sync.py")
        print(f"uploaded app/te_rewrite_sync.py -> {REMOTE}/app/", flush=True)
        if local_nfq.is_file():
            sftp.put(str(local_nfq), f"{REMOTE}/te_rewrite_nfqueue.py")
            print(f"uploaded te_rewrite_nfqueue.py -> {REMOTE}/", flush=True)
    finally:
        sftp.close()

    verify = f"""
set -e
cd {REMOTE}
export MTR_OP_DB={REMOTE}/data.db
export MTR_TE_REWRITE_OIF={down}
export MTR_TE_REWRITE_IIF={up}
export MTR_TE_REWRITE_OUTPUT={te_out}
export MTR_OP_DOWNSTREAM_IFACE={down}
export MTR_BGP_RR_UPLINK_IFACE={up}
export MTR_TE_REWRITE_SCRIPT={REMOTE}/te_rewrite_nfqueue.py
if [ -x ./venv/bin/python ]; then PY=./venv/bin/python; else PY=python3; fi

echo "=== sync te_rewrite (OUTPUT NFQUEUE) ==="
$PY - <<'PY'
import os
from pathlib import Path
import sys
sys.path.insert(0, "{REMOTE}")
from app import storage
from app import te_rewrite_sync
db = Path(os.environ["MTR_OP_DB"])
conn = storage.connect(db)
storage.init_schema(conn)
g = storage.get_global(conn)
print("hijack_enabled=", g.hijack_enabled)
te_rewrite_sync.sync_te_rewrite_from_conn(conn)
conn.close()
print("sync_ok")
PY

echo
echo "=== te_rewrite_nfqueue ==="
pgrep -af te_rewrite_nfqueue || echo "(not running)"
grep -E 'te_rewrite_nfqueue:|43\\.209|201\\.201' /tmp/te_rewrite_nfqueue.log 2>/dev/null | tail -5 || true

echo
echo "=== mtr_te_map.env (209 rule sample) ==="
grep -E '43\\.209|201\\.201|MTR_TE_REWRITE_MAP' /tmp/mtr_te_map.env 2>/dev/null | head -3 || echo "(no map)"

echo
echo "=== iptables mangle FORWARD ==="
iptables -t mangle -S FORWARD 2>/dev/null | grep -E 'NFQUEUE|time-exceeded|{down}|{up}' || true

echo
echo "=== iptables mangle OUTPUT (本机 TE) ==="
iptables -t mangle -S OUTPUT 2>/dev/null | grep -E 'NFQUEUE|time-exceeded|{down}|{up}' || echo "(无 OUTPUT TE 规则 — 检查 MTR_TE_REWRITE_OUTPUT 或 hijack/规则)"

echo
echo "=== OUTPUT 计数 ==="
iptables -t mangle -L OUTPUT -n -v 2>/dev/null | grep -E 'time-exceeded|NFQUEUE' || true

echo
echo "=== done ==="
"""
    code, out = run(c, verify, timeout=90)
    print(out, end="")
    c.close()
    if code != 0:
        raise SystemExit(code)
    print("deploy_te_rewrite_output_only_ok", flush=True)


if __name__ == "__main__":
    main()
