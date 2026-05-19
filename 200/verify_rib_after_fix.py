#!/usr/bin/env python3
import os
import time
from pathlib import Path
import paramiko

pw = "1234qwer"
for line in (Path(__file__).parent / "lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()
pw = os.environ.get("MTR_OP_SSH_PASSWORD", pw)


def snap() -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.133.151.200", username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    cmd = (
        "curl -sf 'http://127.0.0.1:9179/api/rib/routes/count?window=upstream&vrf=gobgp-rr&neighbor_ip=10.133.153.204'; "
        "echo; "
        "curl -sf http://127.0.0.1:9179/api/rr/status | python3 -c \"import json,sys;d=json.load(sys.stdin);print('rr',d.get('rx_status',{}).get('rr_connected'),d.get('rx_status',{}).get('rr_peers'));\"; "
        "journalctl -u bgp-agent -n 5 --no-pager 2>/dev/null | grep background | tail -2"
    )
    _, o, e = c.exec_command(cmd, timeout=30)
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out.strip()


print("等待 RR Established + background ingest（每 30s 采样，最多 15 分钟）…")
for i in range(30):
    out = snap()
    print(f"[{i*30}s] {out}")
    if "background ingest" in out and "ingested=" in out:
        break
    if '"count":' in out:
        import json

        try:
            cnt = json.loads(out.splitlines()[0]).get("count", 0)
            if cnt > 900000:
                print("OK cached >= 900k")
                break
        except Exception:
            pass
    time.sleep(30)
