#!/usr/bin/env python3
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.133.151.200", username="root", password="1234qwer", timeout=25, allow_agent=False, look_for_keys=False)
_, o, _ = c.exec_command(
    "curl -s http://127.0.0.1:8808/api/bgp/neighbors; echo '---'; "
    "curl -s http://127.0.0.1:9179/api/rr/status",
    timeout=30,
)
print(o.read().decode("utf-8", "replace"))
c.close()
