#!/usr/bin/env python3
"""按 OP 流程：ARP 引流 + BGP 邻居添加，并验收 vbgp/ens192 会话。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LAB = Path(__file__).resolve().parent
SPOOF = "10.133.153.204"
VRF = "vbgp10133153204"
PEER = "10.133.152.204"
IFACE = "ens192"
AS = 63199


def load_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def api(method: str, path: str, body: dict | None = None, timeout: int = 120) -> tuple[int, object]:
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    port = os.environ.get("MTR_OP_PORT", "8808")
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw[:600]}


def ssh_diag() -> str:
    import paramiko

    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")
    script = f"""
ip link set ens192 up; ip link set ens224 up
[ -f /root/mtr_op/ensure_uplink_addrs.sh ] && bash /root/mtr_op/ensure_uplink_addrs.sh
ip -br link show ens192 iv204 2>/dev/null; ip -br addr show iv204 2>/dev/null
ip route show vrf {VRF} | head -5
ip neigh show dev iv204 | head -5
ping -c1 -W2 {PEER} || true
ip vrf exec {VRF} ping -c2 -W2 {PEER} || true
ss -tnp | grep {PEER} | head -8
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('vrf')=='{VRF}' or n.get('address')=='{PEER}':
    print(n.get('vrf'), n.get('address'), n.get('state'), 'la', n.get('local_address'), 'rcvd', n.get('pfx_rcd'))
"
curl -sf http://127.0.0.1:9179/api/peers/freeze-status | python3 -c "
import json,sys
j=json.load(sys.stdin)
print('upstream_any_up', j.get('upstream_any_up'))
for p in j.get('downstream',[]):
  if p.get('vrf')=='{VRF}': print(p)
"
"""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(os.environ["MTR_OP_HOST"], username="root", password=pw, timeout=45, banner_timeout=45)
    _, o, e = c.exec_command("bash -se", timeout=90)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    load_env()
    print("=== 1) 启用 ARP 引流 ===")
    c, j = api("PUT", "/api/arp-spoof/settings", {"arp_spoof_enabled": True})
    print(c, j)

    print("\n=== 2) 添加 ARP 条目 (gateway_only / ens192) ===")
    arp_body = {
        "spoof_gateway_ip": SPOOF,
        "satellite_vrf": VRF,
        "egress_iface": IFACE,
        "enabled": True,
        "policy_mode": "gateway_only",
        "policy_cidrs": "",
        "note": "BGPSAT",
    }
    c, j = api("POST", "/api/arp-spoof/targets", arp_body)
    print(c, json.dumps(j, ensure_ascii=False, indent=2) if isinstance(j, dict) else j)
    if c == 409:
        print("(已存在则继续)")

    print("\n=== 3) 添加 BGP 邻居 (下游) ===")
    bgp_body = {
        "vrf": VRF,
        "neighbor_ip": PEER,
        "remote_as": AS,
        "role": "downstream",
        "source_ip": SPOOF,
        "create_kernel_vrf_if_missing": True,
    }
    c, j = api("POST", "/api/bgp/neighbors", bgp_body, timeout=180)
    print(c, json.dumps(j, ensure_ascii=False, indent=2) if isinstance(j, dict) else j)
    if c >= 400:
        print("BGP add failed", file=sys.stderr)
        return 1

    print("\n=== 4) restore / unfreeze ===")
    c, j = api("POST", "/api/bgp/restore-agent", {}, timeout=300)
    print("restore", c, str(j)[:400])
    api("POST", "/api/gobgp/unfreeze", {})

    print("\n等待 40s …")
    time.sleep(40)

    print("\n=== 5) OP 邻居列表 ===")
    c, neighbors = api("GET", "/api/bgp/neighbors", timeout=60)
    if c == 200 and isinstance(neighbors, list):
        for n in neighbors:
            if n.get("vrf") == VRF or n.get("neighbor_ip") == PEER:
                print(json.dumps(n, ensure_ascii=False, indent=2))

    print("\n=== 6) 200 主机诊断 ===")
    print(ssh_diag())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
