#!/usr/bin/env python3
"""201 上 mtr -a 本机 152.204 时，ICMP TE 直达 201，在 201 INPUT NFQUEUE 改写外层源。"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
ROOT = LAB.parent
SCRIPT = ROOT / "scripts" / "te_rewrite_nfqueue.py"
H200, H201 = "10.133.151.200", "10.133.151.201"
REMOTE = "/root/te_rewrite_nfqueue.py"
QUEUE = "2"


def load_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def fetch_map() -> str:
    url = f"http://{H200}:8808/api/hop-rules"
    with urllib.request.urlopen(url, timeout=15) as r:
        rules = json.load(r)
    parts: list[str] = []
    for row in rules:
        if not row.get("enabled", True):
            continue
        mc = (row.get("match_cidr") or "").strip()
        fg = (row.get("forged_src") or "").strip()
        if not mc or not fg:
            continue
        parts.append(f"{mc.split('/')[0].strip()}={fg}")
    return ",".join(parts)


def run(host: str, script: str, pw: str, timeout: int = 120) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not pw:
        print("need MTR_OP_SSH_PASSWORD", file=sys.stderr)
        return 2
    try:
        map_line = fetch_map()
    except Exception as e:
        print(f"fetch hop-rules from 200 failed: {e}", file=sys.stderr)
        return 1
    if not map_line:
        print("no enabled hop rules on 200", file=sys.stderr)
        return 1

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H201, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    c.open_sftp().put(str(SCRIPT), REMOTE)
    c.close()

    remote = f"""
set -e
apt-get install -y -qq python3-scapy libnetfilter-queue1 2>/dev/null || true
python3 -c 'import netfilterqueue, scapy' || {{
  echo 'run: python 200/_copy_nfq_201.py  (copy NetfilterQueue from 200)'; exit 2
}}
modprobe nfnetlink_queue 2>/dev/null || true
pkill -f '{REMOTE}' 2>/dev/null || true
iptables -t mangle -D INPUT -p icmp -m icmp --icmp-type time-exceeded -j NFQUEUE --queue-num {QUEUE} 2>/dev/null || true
iptables -t mangle -I INPUT 1 -p icmp -m icmp --icmp-type time-exceeded -j NFQUEUE --queue-num {QUEUE}
cat > /tmp/mtr_te_map.env <<'MAPENV'
export MTR_TE_REWRITE_MAP='{map_line}'
MAPENV
export MTR_TE_REWRITE_MAP='{map_line}'
export MTR_TE_QUEUE_NUM={QUEUE}
nohup python3 {REMOTE} >> /tmp/te_rewrite_nfqueue.log 2>&1 &
sleep 1
echo '--- map ---'
cat /tmp/mtr_te_map.env
tail -2 /tmp/te_rewrite_nfqueue.log 2>/dev/null || true
pgrep -af te_rewrite_nfqueue || exit 3
mtr -4 -r -n -m 6 -c 2 -a 10.133.152.204 -I ens192 210.73.209.82 2>&1 | head -12
"""
    out = run(H201, remote, pw)
    print(out)
    if "200.200.200.200" in out or "100.100.100.100" in out:
        print("sync_201_te_rewrite_ok")
        return 0
    if "10.133.151.210" in out and "200.200.200.200" not in out:
        print("WARN: hop 2 still 151.210 — check /tmp/mtr_te_map.env on 201", file=sys.stderr)
    return 0 if "te_rewrite_nfqueue" in out and "151.210=200.200.200.200" in out else 1


if __name__ == "__main__":
    sys.exit(main())
