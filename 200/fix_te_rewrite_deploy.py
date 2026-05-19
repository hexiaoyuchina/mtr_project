#!/usr/bin/env python3
"""Linux 200：部署 te_rewrite_nfqueue.py、安装依赖并触发 OP 同步。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
ROOT = LAB.parent
SCRIPT_LOCAL = ROOT / "scripts" / "te_rewrite_nfqueue.py"
REMOTE = "/root/mtr_op"
HOST = os.environ.get("MTR_OP_HOST", "10.133.151.200").strip()


def load_env() -> None:
    env_file = LAB / "lab.env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not pw:
        print("need MTR_OP_SSH_PASSWORD", file=sys.stderr)
        return 2
    if not SCRIPT_LOCAL.is_file():
        print(f"missing {SCRIPT_LOCAL}", file=sys.stderr)
        return 1

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    remote_script = f"{REMOTE}/te_rewrite_nfqueue.py"
    sftp.put(str(SCRIPT_LOCAL), remote_script)
    sftp.close()

    remote_py = r"""
set -e
if ! python3 -c 'import netfilterqueue, scapy' 2>/dev/null; then
  apt-get update -qq && apt-get install -y -qq python3-netfilterqueue python3-scapy 2>/dev/null || true
fi
if ! python3 -c 'import netfilterqueue' 2>/dev/null; then
  if [ -x /root/mtr_op/venv/bin/pip ]; then
    /root/mtr_op/venv/bin/pip install -q NetfilterQueue scapy 2>/dev/null || true
  fi
fi
python3 -c 'import netfilterqueue, scapy' || { echo 'MISSING netfilterqueue/scapy'; exit 2; }
modprobe nfnetlink_queue 2>/dev/null || true
cd /root/mtr_op
export MTR_OP_DB=/root/mtr_op/data.db
python3 - <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, "/root/mtr_op")
from app import storage, te_rewrite_sync
conn = storage.connect(Path(os.environ["MTR_OP_DB"]))
storage.init_schema(conn)
te_rewrite_sync.sync_te_rewrite_from_conn(conn)
conn.close()
print("sync_ok")
PY
echo '--- map ---'
cat /tmp/mtr_te_map.env
echo '--- procs ---'
pgrep -af te_rewrite_nfqueue || echo NO_DAEMON
echo '--- iptables mangle FORWARD (head) ---'
iptables -t mangle -L FORWARD -n -v 2>&1 | head -8
echo '--- te log tail ---'
tail -6 /tmp/te_rewrite_nfqueue.log 2>&1 || true
echo '--- hop rules ---'
curl -sf http://127.0.0.1:8808/api/hop-rules | python3 -c "import sys,json; d=json.load(sys.stdin); print([(x.get('match_cidr'),x.get('forged_src'),x.get('enabled')) for x in d])" 2>/dev/null || curl -sf http://127.0.0.1:8808/api/hop-rules | head -c 400
echo
curl -sf http://127.0.0.1:8808/api/global | python3 -c "import sys,json; g=json.load(sys.stdin); print('hijack_enabled', g.get('hijack_enabled'))" 2>/dev/null || true
"""
    _, o, e = c.exec_command("bash -se", timeout=120)
    o.channel.send(remote_py.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    print(out)
    if "sync_ok" not in out:
        return 1
    if "10.131.61.1=100.100.100.100" not in out and "10.131.61.1" not in out:
        print("WARN: map may not contain 10.131.61.1 rule", file=sys.stderr)
    if "NO_DAEMON" in out:
        return 1
    print("fix_te_rewrite_ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
