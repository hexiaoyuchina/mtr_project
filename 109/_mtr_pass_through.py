#!/usr/bin/env python3
"""109：关掉 NFQUEUE + 逐跳总开关，先保证 MTR 能出跳。"""
from __future__ import annotations

import os
from pathlib import Path

import paramiko

DIR = Path(__file__).resolve().parent


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


def main() -> None:
    load_env()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ["MTR_OP_HOST"].strip(),
        username="root",
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    script = r"""
pkill -9 -f te_rewrite_nfqueue 2>/dev/null || true
cd /root/mtr_op
./venv/bin/python3 -c "
import sys; sys.path.insert(0,'.')
from app import te_rewrite_sync
te_rewrite_sync.clear_iptables_nfqueue()
print('nfqueue_cleared')
"
# 关总开关，避免 uvicorn 再起 NFQUEUE
curl -sf -X PUT http://127.0.0.1:8808/api/global \
  -H 'Content-Type: application/json' -d '{"hijack_enabled":false}'
echo
iptables -t mangle -S FORWARD
echo "请立即在 208 上重试 mtr"
"""
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=30)
    stdin.write(script)
    stdin.channel.shutdown_write()
    print((stdout.read() + stderr.read()).decode(errors="replace"))
    c.close()


if __name__ == "__main__":
    main()
