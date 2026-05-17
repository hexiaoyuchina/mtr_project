#!/usr/bin/env python3
"""本端修复：路由、静态邻居、DNAT 249:179、ARP 守护、被动 BGP。"""
import json
import os
import sys
import time
from pathlib import Path

import paramiko

HOST = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
PW = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
OP_DIR = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").strip()
ROOT = Path(__file__).resolve().parent.parent


def run(c, cmd, timeout=120):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    return o.channel.recv_exit_status(), o.read().decode("utf-8", "replace"), e.read().decode("utf-8", "replace")


def main():
    if not PW:
        print("set MTR_OP_SSH_PASSWORD", file=sys.stderr)
        sys.exit(2)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username="root", password=PW, timeout=20)
    sftp = c.open_sftp()
    uploads = [
        (ROOT / "service/app/bgp_ipvlan_reconcile.py", f"{OP_DIR}/app/bgp_ipvlan_reconcile.py"),
        (ROOT / "service/app/bgp_control.py", f"{OP_DIR}/app/bgp_control.py"),
        (ROOT / "service/app/main.py", f"{OP_DIR}/app/main.py"),
        (ROOT / "service/bgp_agent/api_neighbors.go", f"{OP_DIR}/bgp_agent/api_neighbors.go"),
        (ROOT / "service/bgp_agent/pkg/tx/pool.go", f"{OP_DIR}/bgp_agent/pkg/tx/pool.go"),
        (ROOT / "service/bgp_agent/pkg/tx/tx_agent.go", f"{OP_DIR}/bgp_agent/pkg/tx/tx_agent.go"),
        (ROOT / "service/bgp_agent/pkg/rx/rx_agent.go", f"{OP_DIR}/bgp_agent/pkg/rx/rx_agent.go"),
    ]
    for lp, rp in uploads:
        sftp.put(str(lp), rp)
    arp_daemon = ROOT / "scripts" / "arp_spoof_daemon.py"
    if arp_daemon.is_file():
        sftp.put(str(arp_daemon), f"{OP_DIR}/arp_spoof_daemon.py")
    sftp.close()

    print("=== rebuild bgp-agent ===")
    run(
        c,
        f"cd {OP_DIR}/bgp_agent && export PATH=/usr/local/go/bin:$PATH && "
        "go build -o bgp_agent -ldflags='-s -w' . && systemctl restart bgp-agent && sleep 3",
        timeout=180,
    )

    remote_py = f"""import json, os, sys
from pathlib import Path
os.environ["MTR_BGP_IPVLAN_PEER_IP"] = "139.159.43.208"
os.environ["MTR_SATELLITE_PEER_IP"] = "139.159.43.208"
os.environ["MTR_BGP_IPVLAN_BASE_IFACE"] = "eno1np0"
os.environ["MTR_BGP_RR_UPLINK_IFACE"] = "enp59s0f0np0"
os.environ["MTR_BGP_IPVLAN_AUTO"] = "1"
os.environ["MTR_BGP_RR_SPOOF_PASSIVE"] = "1"
sys.path.insert(0, {OP_DIR!r})
from app import bgp_ipvlan_reconcile, arp_spoof_assign
db = Path({OP_DIR!r}) / 'data.db'
arp_spoof_assign.reconcile_from_op_database(db)
r = bgp_ipvlan_reconcile.reconcile_from_op_database(db)
print('reconcile', json.dumps(r, ensure_ascii=False)[:2500])
"""
    sftp = c.open_sftp()
    sftp.file("/tmp/mtr_repair2.py", "w").write(remote_py)
    sftp.close()
    print("=== reconcile + arp assign ===")
    _, out, err = run(c, "python3 /tmp/mtr_repair2.py")
    print(out[:3500])
    if err.strip():
        print(err[:400])

    print("=== arp_spoof_daemon ===")
    run(c, "pkill -f arp_spoof_daemon.py 2>/dev/null || true")
    _, out, _ = run(
        c,
        f"nohup python3 {OP_DIR}/arp_spoof_daemon.py --op-db {OP_DIR}/data.db --verbose "
        f">> /tmp/arp_spoof_daemon.log 2>&1 & sleep 2; pgrep -af arp_spoof_daemon || true",
    )
    print(out[:500])

    agent = "http://127.0.0.1:9179"
    run(c, f"curl -sf -X POST -H 'Content-Type: application/json' -d '{{\"address\":\"139.159.43.249\",\"remote_as\":63199,\"local_address\":\"139.159.43.207\"}}' {agent}/api/rr/config")
    run(
        c,
        f"curl -sf -X POST -H 'Content-Type: application/json' "
        f"-d '{{\"address\":\"139.159.43.208\",\"vrf\":\"vbgp13915943249\"}}' {agent}/api/neighbors/remove",
    )
    for peer, passive in (("139.159.43.208", False), ("139.159.43.204", True)):
        body = json.dumps(
            {
                "address": peer,
                "remote_as": 63199,
                "role": "downstream",
                "vrf": "vbgp13915943249",
                "local_address": "139.159.43.249",
                "bind_interface": "iv249",
                "passive_mode": passive,
            }
        )
        _, out, _ = run(
            c,
            f"curl -sf -X POST -H 'Content-Type: application/json' -d '{body}' {agent}/api/neighbors/add",
        )
        print(f"add {peer}:", out)
    verify = r"""
sleep 8
echo "=== nft dnat ==="
nft list chain inet mtr_bgp_spoof_rr prerouting 2>/dev/null || true
echo "=== neigh 208 iv249 ==="
ip neigh show dev iv249 | grep 208 || true
echo "=== ss ==="
ss -tn | grep -E '208|204|1830' || true
timeout 8 tcpdump -ni any -c 12 'tcp port 1830' 2>&1 | tail -8
echo "=== neighbors ==="
curl -sf http://127.0.0.1:9179/api/neighbors
echo ""
echo "=== tcpdump 12s 204/208 -> 249 ==="
timeout 12 tcpdump -ni enp59s0f0np0 -c 25 'host 139.159.43.249 and tcp port 179' 2>&1 | tail -15
"""
    sftp = c.open_sftp()
    sftp.file("/tmp/mtr_v2.sh", "w").write(verify)
    sftp.close()
    _, out, _ = run(c, "bash /tmp/mtr_v2.sh", timeout=40)
    print(out)
    c.close()


if __name__ == "__main__":
    main()
