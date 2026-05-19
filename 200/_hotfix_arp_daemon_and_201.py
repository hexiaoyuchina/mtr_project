#!/usr/bin/env python3
"""上传 arp_spoof_daemon；恢复 201 table 2001；验证 ping 249。"""
import os
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
LAB = Path(__file__).resolve().parent
for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

pw = os.environ["MTR_OP_SSH_PASSWORD"]
REMOTE = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
H200, H201 = "10.133.151.200", "10.133.151.201"
MAC200 = "00:50:56:af:97:a6"


def ssh(host: str, script: str, timeout: int = 90) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    return (o.read() + e.read()).decode("utf-8", "replace")


c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(H200, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
sftp = c.open_sftp()
try:
    sftp.stat(f"{REMOTE}/scripts")
except OSError:
    sftp.mkdir(f"{REMOTE}/scripts")
sftp.put(str(ROOT / "scripts" / "arp_spoof_daemon.py"), f"{REMOTE}/scripts/arp_spoof_daemon.py")
sftp.close()
c.close()

policy = (ROOT / "scripts" / "linux201_src152_policy_route.sh").read_text(encoding="utf-8")
print("=== 200: start arp daemon ===")
print(
    ssh(
        H200,
        f"""
export MTR_OP_DB={REMOTE}/data.db
pkill -f arp_spoof_daemon.py 2>/dev/null || true
cd {REMOTE}
nohup ./venv/bin/python3 scripts/arp_spoof_daemon.py --op-db $MTR_OP_DB >>/tmp/arp_spoof_daemon.log 2>&1 &
sleep 2
ps aux | grep arp_spoof_daemon | grep -v grep | head -1
tail -3 /tmp/arp_spoof_daemon.log 2>/dev/null || true
""",
    )
)

print("=== 201: table 2001 + neigh ===")
print(ssh(H201, policy))
print(
    ssh(
        H201,
        f"""
sysctl -w net.ipv4.conf.ens192.rp_filter=0
ip neigh replace 10.133.152.200 lladdr {MAC200} dev ens192 nud permanent
sleep 3
ip neigh show 10.133.152.249 dev ens192 || true
ping -c3 -W2 -a 10.133.152.204 -I ens192 10.133.152.249
""",
        timeout=35,
    )
)
