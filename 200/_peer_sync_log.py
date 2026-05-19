import os
from pathlib import Path
import paramiko

for line in Path("lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(
    "10.133.151.200",
    username="root",
    password=os.environ["MTR_OP_SSH_PASSWORD"],
    timeout=20,
    allow_agent=False,
    look_for_keys=False,
)
_, o, e = c.exec_command("grep -E te_rewrite_peer /tmp/mtr_op.log | tail -15", timeout=15)
print((o.read() + e.read()).decode())
c.close()
