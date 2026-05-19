import os
import shutil
import tarfile
import tempfile
from pathlib import Path

import paramiko

for line in Path(__file__).with_name("lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

pw = os.environ["MTR_OP_SSH_PASSWORD"]


def run(host: str, cmd: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command(cmd, timeout=60)
    out = (o.read() + e.read()).decode()
    c.close()
    return out


pkg_dir = "/usr/local/lib/python3.8/dist-packages"
check = run("10.133.151.200", f"test -d {pkg_dir}/netfilterqueue && echo ok").strip()
if check != "ok":
    raise SystemExit(f"netfilterqueue missing under {pkg_dir} on 200")

c200 = paramiko.SSHClient()
c200.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c200.connect("10.133.151.200", username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
_, stdout, _ = c200.exec_command(
    f"tar czf - -C {pkg_dir} netfilterqueue netfilterqueue-1.1.0.dist-info",
    timeout=120,
)
data = stdout.read()
c200.close()

with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tf:
    tf.write(data)
    tgz = tf.name

dest = "/usr/local/lib/python3.8/dist-packages"
c201 = paramiko.SSHClient()
c201.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c201.connect("10.133.151.201", username="root", password=pw, timeout=25, allow_agent=False, look_for_keys=False)
sftp = c201.open_sftp()
try:
    c201.exec_command(f"mkdir -p {dest}")[1].channel.recv_exit_status()
    sftp.put(tgz, "/tmp/nfq_bundle.tgz")
finally:
    sftp.close()
_, o, e = c201.exec_command(f"tar xzf /tmp/nfq_bundle.tgz -C {dest} && rm /tmp/nfq_bundle.tgz", timeout=60)
print((o.read() + e.read()).decode())
_, o, e = c201.exec_command(
    "apt-get install -y -qq libnetfilter-queue1 2>/dev/null || true; "
    "python3 -c 'import netfilterqueue as n; print(\"ok\", n.__file__)'",
    timeout=120,
)
print((o.read() + e.read()).decode())
c201.close()
os.unlink(tgz)
