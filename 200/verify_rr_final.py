#!/usr/bin/env python3
"""OP/Agent 验收 RR 会话。"""
import json
import sys
import time
import urllib.request

import paramiko

H200, PW = "10.133.151.200", "1234qwer"
RR, LOCAL = "10.133.153.204", "10.133.153.200"


def http(url, method="GET", body=None, timeout=30):
    data = None
    h = {"Accept": "application/json"}
    if body:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ssh(script: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H200, username="root", password=PW, timeout=25, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=120)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    # via SSH tunnel local - use ssh curl
    print(ssh(r"""
set -e
bash /root/mtr_op/remote-network-prereq.sh 2>/dev/null || true
curl -sf -X POST http://127.0.0.1:9179/api/rr/unfreeze || true
curl -sf -X POST http://127.0.0.1:9179/api/rr/config -H 'Content-Type: application/json' \
  -d '{"address":"10.133.153.204","remote_as":63199,"local_address":"10.133.153.200"}'
echo configured
"""))
    for i in range(6):
        time.sleep(5)
        out = ssh(
            "curl -s http://127.0.0.1:9179/api/rr/status; echo; "
            "ss -tn state established '( sport = :179 or dport = :179 )' | grep -E '153.200|153.204' || true; echo '---'"
        )
        print(f"--- t+{(i+1)*5}s ---")
        print(out)
        if "ESTABLISHED" in out.upper() and "rr_state" in out:
            if '"rr_state":"BGP_FSM_ESTABLISHED"' in out.replace(" ", "") or "BGP_FSM_ESTABLISHED" in out:
                print("PASS")
                return 0
    print("FAIL: RR not ESTABLISHED after 30s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
