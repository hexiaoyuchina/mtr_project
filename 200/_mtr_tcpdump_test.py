import os
import threading
import time
from pathlib import Path

import paramiko

for line in Path(__file__).with_name("lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

pw = os.environ["MTR_OP_SSH_PASSWORD"]


def ssh(host: str):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    return c


def run_cmd(c, cmd, timeout=60):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    return (o.read() + e.read()).decode()


cap = {"224": "", "160": ""}


def capture():
    c = ssh("10.133.151.200")
    _, o, _ = c.exec_command(
        "timeout 15 tcpdump -ni ens224 -c 6 'icmp and host 210.73.209.82' 2>&1; "
        "echo '---'; timeout 15 tcpdump -ni ens160 -c 6 'icmp and host 210.73.209.82' 2>&1",
        timeout=25,
    )
    cap["out"] = o.read().decode()
    c.close()


t = threading.Thread(target=capture)
t.start()
time.sleep(2)
c201 = ssh("10.133.151.201")
mtr = run_cmd(
    c201,
    "mtr -4 -r -n -m 5 -c 2 -a 10.133.152.204 -I ens192 210.73.209.82 2>&1 | head -12",
    timeout=40,
)
c201.close()
t.join(timeout=30)
print("=== mtr ===")
print(mtr)
print("=== tcpdump 200 ===")
print(cap.get("out", ""))
