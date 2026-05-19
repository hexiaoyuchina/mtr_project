#!/usr/bin/env python3
"""
标准流程（10.133.152.235 实例）— 仅通过 OP API：

1. POST ARP 引流（ens192）→ 201 可 ping 10.133.152.235
2. POST BGP 邻居（卫星 VRF + TCP 源 152.235）→ 201 show bgp 见 Established

用法：python 200/op_flow_235.py
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
SPOOF = "10.133.152.235"
VRF = "vbgp10133152235"
PEER = "10.133.152.204"
IFACE = "ens192"
AS = 63199


def load_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def api(method: str, path: str, body: dict | None = None, timeout: int = 180):
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    port = os.environ.get("MTR_OP_PORT", "8808")
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw[:500]}


def ssh(host: str, script: str, timeout: int = 60) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=os.environ["MTR_OP_SSH_PASSWORD"], timeout=45)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    return out


def main() -> int:
    load_env()
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")

    print("=== 0) 同步 OP 代码到 200 ===")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.133.151.200", username="root", password=os.environ["MTR_OP_SSH_PASSWORD"], timeout=45)
    sftp = c.open_sftp()
    for rel in ("service/app/main.py", "service/app/bgp_ipvlan_reconcile.py"):
        sftp.put(str(LAB.parent / rel), f"{remote}/app/{Path(rel).name}")
    sftp.close()
    c.exec_command("systemctl restart mtr-op")
    c.close()
    time.sleep(4)

    print("\n=== 1) ARP 引流 ===")
    api("PUT", "/api/arp-spoof/settings", {"arp_spoof_enabled": True})
    c, j = api(
        "POST",
        "/api/arp-spoof/targets",
        {
            "spoof_gateway_ip": SPOOF,
            "satellite_vrf": VRF,
            "egress_iface": IFACE,
            "enabled": True,
            "policy_mode": "gateway_only",
            "note": "BGPSAT",
        },
    )
    print("ARP", c, j.get("satellite_vrf") or j)

    print("\n=== 201 ping ===")
    print(
        ssh(
            "10.133.151.201",
            f"""
ip link set ens192 up
ip route replace {SPOOF}/32 dev ens192 scope link 2>/dev/null || true
ping -c3 -W2 {SPOOF}
""",
        )
    )

    print("\n=== 2) BGP 下游邻居 ===")
    api("DELETE", f"/api/bgp/neighbors/{VRF}/{PEER}")
    c, j = api(
        "POST",
        "/api/bgp/neighbors",
        {
            "vrf": VRF,
            "neighbor_ip": PEER,
            "remote_as": AS,
            "role": "downstream",
            "source_ip": SPOOF,
            "bgp_local_as": AS,
            "bgp_router_id": SPOOF,
            "create_kernel_vrf_if_missing": True,
        },
        timeout=180,
    )
    print("BGP", c, j.get("session_state") if isinstance(j, dict) else j)

    print("\n等待 20s…")
    time.sleep(20)

    print(
        ssh(
            "10.133.151.200",
            f"""
ip route get {PEER} from {SPOOF}
ss -tnp | grep 152.204 | grep 235
curl -sf --max-time 8 http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('vrf')=='{VRF}': print(n)
"
""",
        )
    )
    print(
        ssh(
            "10.133.151.201",
            f"vtysh -c 'show bgp summary' 2>/dev/null | grep {SPOOF} || true",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
