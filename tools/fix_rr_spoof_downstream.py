#!/usr/bin/env python3
"""现网：修正 249 ipvlan 出接口、策略路由，恢复 RR + 冒充 RR 下游会话。"""
import json
import os
import sys
import time
from pathlib import Path

import paramiko

HOST = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
PW = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
OP_DIR = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").strip()
BASE_IF = os.environ.get("MTR_BGP_IPVLAN_BASE_IFACE", "enp59s0f0np0").strip()
PEER = os.environ.get("MTR_SATELLITE_PEER_IP", "139.159.43.208").strip()
SPOOF = "139.159.43.249"
VRF = "vbgp13915943249"
RR = "139.159.43.249"
RR_SRC = "139.159.43.207"
AGENT = "http://127.0.0.1:9179"
OP = "http://127.0.0.1:8808"


def run(c, cmd, timeout=120):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    code = o.channel.recv_exit_status()
    return code, out, err


def curl_json(c, method, url, body=None):
    if body is None:
        cmd = f"curl -sf -X {method} '{url}'"
    else:
        j = json.dumps(body).replace("'", "'\\''")
        cmd = f"curl -sf -X {method} -H 'Content-Type: application/json' -d '{j}' '{url}'"
    return run(c, cmd)


def main():
    if not PW:
        print("set MTR_OP_SSH_PASSWORD", file=sys.stderr)
        sys.exit(2)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username="root", password=PW, timeout=20)

    remote_py = f"/tmp/mtr_fix_arp.py"
    py_src = f"""import json, sqlite3, sys
from pathlib import Path
sys.path.insert(0, {OP_DIR!r})
from app import bgp_ipvlan_reconcile
db = Path({OP_DIR!r}) / 'data.db'
conn = sqlite3.connect(str(db))
conn.execute(
    'UPDATE arp_spoof_targets SET egress_iface=?, satellite_vrf=? WHERE spoof_gateway_ip=?',
    ({BASE_IF!r}, {VRF!r}, {SPOOF!r}),
)
print('arp_rows_updated', conn.total_changes)
conn.commit()
conn.close()
r = bgp_ipvlan_reconcile.reconcile_from_op_database(db)
print(json.dumps(r, ensure_ascii=False)[:3000])
"""
    sftp = c.open_sftp()
    local_ipvlan = Path(__file__).resolve().parent.parent / "service" / "app" / "bgp_ipvlan_reconcile.py"
    sftp.put(str(local_ipvlan), f"{OP_DIR}/app/bgp_ipvlan_reconcile.py")
    with sftp.file(remote_py, "w") as f:
        f.write(py_src)
    sftp.close()

    print("=== ARP + reconcile ===")
    code, out, err = run(c, f"python3 {remote_py}")
    print(out[:4000])
    if err.strip():
        print("stderr:", err[:400])

    print("=== RR config ===")
    code, out, err = curl_json(
        c, "POST", f"{AGENT}/api/rr/config", {"address": RR, "remote_as": 63199, "local_address": RR_SRC}
    )
    print(out or err)

    print("=== downstream remove/add ===")
    curl_json(c, "POST", f"{AGENT}/api/neighbors/remove", {"address": PEER, "vrf": "default"})
    curl_json(c, "POST", f"{AGENT}/api/neighbors/remove", {"address": PEER, "vrf": VRF})
    code, out, err = curl_json(
        c,
        "POST",
        f"{AGENT}/api/neighbors/add",
        {
            "address": PEER,
            "remote_as": 63199,
            "role": "downstream",
            "vrf": VRF,
            "local_address": SPOOF,
            "bind_interface": "iv249",
        },
    )
    print(out or err)

    time.sleep(4)
    checks = [
        "ip link show iv249",
        "ip -4 rule show | grep 139.159.43.249 || true",
        f"ip vrf exec {VRF} ip route get {PEER} from {SPOOF}",
        "ss -tn | grep 139.159.43.208 || true",
        f"curl -sf {AGENT}/api/neighbors",
        f"curl -sf {AGENT}/api/status",
    ]
    for cmd in checks:
        code, out, err = run(c, cmd)
        print("---", cmd)
        print((out or err)[:1500])

    c.close()


if __name__ == "__main__":
    main()
