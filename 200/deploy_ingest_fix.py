#!/usr/bin/env python3
"""部署 ingest 流式修复并触发 RR 全量灌库。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

LAB = Path(__file__).resolve().parent
ROOT = LAB.parent
sys.path.insert(0, str(ROOT / "tools"))

from deploy_light import REMOTE, connect, run_script, upload_bgp_agent_tree, upload_tree  # noqa: E402
from bgp_agent_remote import shell_sync_bgp_agent  # noqa: E402

SERVICE = ROOT / "service"
SKIP_DIRS = {".git", "__pycache__", "venv", ".venv", "node_modules"}


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def main() -> int:
    load_lab_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "")
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
    if not pw:
        print("need MTR_OP_SSH_PASSWORD", file=sys.stderr)
        return 2

    print("upload service/app ...")
    c, sftp = connect(host, pw)
    try:
        upload_tree(sftp, SERVICE, remote, SKIP_DIRS)
        upload_bgp_agent_tree(sftp, SERVICE / "bgp_agent", f"{remote}/bgp_agent")
    finally:
        sftp.close()
        c.close()

    print("build + restart bgp-agent, sync op ...")
    code, out = run_script(
        host,
        pw,
        f"""
        set -e
        export REMOTE={remote}
        cd $REMOTE/bgp_agent
        export PATH=/usr/local/go/bin:/usr/bin:$PATH
        go build -o bgp_agent .
        systemctl restart bgp-agent
        sleep 3
        curl -sf http://127.0.0.1:9179/health && echo agent_ok
        systemctl restart mtr-op 2>/dev/null || true
        sleep 2
        curl -sf http://127.0.0.1:8808/health && echo op_ok
        """,
        timeout=300,
    )
    print(out)
    if code != 0:
        return code

    print("wait RR established + background ingest (90s)...")
    time.sleep(90)
    code2, out2 = run_script(
        host,
        pw,
        """
        curl -sf 'http://127.0.0.1:9179/api/rib/routes/count?window=upstream&vrf=gobgp-rr&neighbor_ip=10.133.153.204'
        echo
        curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='10.133.153.204': print('pfx_rcd',n.get('pfx_rcd'))"
        journalctl -u bgp-agent -n 15 --no-pager | grep -i background || true
        """,
        timeout=60,
    )
    print(out2)
    return code2


if __name__ == "__main__":
    raise SystemExit(main())
