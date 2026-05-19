#!/usr/bin/env python3
"""关闭探测 SNAT/DNAT，刷新 te_rewrite（移除 NFQUEUE 依赖风险）。"""
import os
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
for line in Path(__file__).with_name("lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

pw = os.environ["MTR_OP_SSH_PASSWORD"]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.133.151.200", username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
sftp = c.open_sftp()
sftp.put(str(ROOT / "service" / "app" / "te_rewrite_sync.py"), "/root/mtr_op/app/te_rewrite_sync.py")
sftp.close()
script = """export MTR_OP_DB=/root/mtr_op/data.db
export MTR_TE_PROBE_RETURN_VIA_200=0
export MTR_TE_REWRITE_PEER_HOSTS=
cd /root/mtr_op
./venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, "/root/mtr_op")
from pathlib import Path
from app import storage, te_rewrite_sync
conn = storage.connect(Path(os.environ["MTR_OP_DB"]))
te_rewrite_sync.sync_te_rewrite_from_conn(conn)
conn.close()
print("sync_ok")
PY
iptables -t nat -L POSTROUTING -n -v | grep 152.204 || echo 'no SNAT'
iptables -t nat -L PREROUTING -n -v | grep 152.200 || echo 'no DNAT'
iptables -t mangle -L FORWARD -n -v | grep NFQUEUE || echo 'mangle NFQUEUE (see rules above if any)'
"""
_, o, e = c.exec_command("bash -se", timeout=60)
o.channel.send(script.encode())
o.channel.shutdown_write()
print((o.read() + e.read()).decode())
_, o, e = c.exec_command(
    "mtr -4 -r -n -m 8 -c 3 -a 10.133.152.204 -I ens192 210.73.209.82 2>&1 | tail -12",
    timeout=120,
)
print("=== mtr on 201 via ssh ===")
print((o.read() + e.read()).decode())
c.close()
