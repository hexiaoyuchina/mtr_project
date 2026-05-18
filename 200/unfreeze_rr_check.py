#!/usr/bin/env python3
import paramiko

PW = "1234qwer"


def ros(cmd: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.133.151.210", username="admin", password=PW, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command(cmd, timeout=60)
    out = o.read().decode() + e.read().decode()
    c.close()
    return out


def root(script: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.133.151.200", username="root", password=PW, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=90)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = o.read().decode() + e.read().decode()
    c.close()
    return out


print("=== ROS peer-lin200-153 ===")
print(ros("/routing bgp peer print detail where name=peer-lin200-153"))
print(ros("/log print where message~\"bgp|153.200\""))

print("=== 200 unfreeze + wait ===")
print(
    root(
        """
curl -sf -X POST http://127.0.0.1:9179/api/rr/unfreeze && echo unfreeze_ok
sleep 10
journalctl -u bgp-agent -n 40 --no-pager | grep -iE '153.204|fsm|connect|estab|fail|error' | tail -20
ss -tnp | grep 153.204 | head -12 || true
curl -s http://127.0.0.1:9179/api/rr/status | python3 -m json.tool 2>/dev/null || curl -s http://127.0.0.1:9179/api/rr/status
"""
    )
)
