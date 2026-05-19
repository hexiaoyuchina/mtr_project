import os
from pathlib import Path
import paramiko

for line in Path(__file__).with_name("lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

pw = os.environ["MTR_OP_SSH_PASSWORD"]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.133.151.200", username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
script = r"""
ip addr show dev ens224 | head -4
ip addr show dev ens192 | head -4
ip route get 210.73.209.82 from 10.133.152.204 iif ens192
timeout 10 tcpdump -ni ens224 -c 8 icmp 2>&1 &
TP=$!
sleep 1
ping -c1 -W1 -I 10.133.152.200 -c 1 10.133.152.204 >/dev/null 2>&1 || true
mtr -4 -r -n -m 4 -c 1 -a 10.133.152.204 10.133.153.204 2>&1 | head -7 || true
wait $TP 2>/dev/null || true
"""
_, o, e = c.exec_command("bash -se", timeout=40)
o.channel.send(script.encode())
o.channel.shutdown_write()
print((o.read() + e.read()).decode())
c.close()
