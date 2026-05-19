#!/usr/bin/env python3
import paramiko

PW = "1234qwer"


def run(host, script, timeout=60):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=PW, timeout=45)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    return (o.read() + e.read()).decode("utf-8", "replace")


print(run("10.133.151.200", """
ss -tlnp | grep -E '1833|1790|bgp'
ss -tnp | grep -E '152.204|1833' | head -10
journalctl -u bgp-agent -n 30 --no-pager | grep -iE '152.204|1833|passive|error|estab' | tail -15
timeout 5 tcpdump -ni ens192 tcp port 179 or tcp port 1833 -c 10 &
sleep 1
ping -c1 -W2 -I 10.133.153.204 10.133.152.204
wait
"""))

print(run("10.133.151.201", """
ss -tnp | grep 153.204 | head -8
vtysh -c 'show bgp neighbors 10.133.153.204' 2>/dev/null | grep -iE 'BGP state|last|remote|local|Timers' | head -12
""", timeout=30))
