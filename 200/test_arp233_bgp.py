#!/usr/bin/env python3
"""实验室：ARP 冒充 10.133.152.233 + BGP（与 index.html 相同 API）。"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import paramiko
except ImportError:
    raise SystemExit(2)

LAB = Path(__file__).resolve().parent
SPOOF_IP = "10.133.152.233"
EGRESS = "ens192"
SAT_VRF = "vbgp10133152233"  # IPv4 去点，与 vrf_naming.satellite_vrf_name 一致
NEIGHBOR_201 = "10.133.152.204"
REMOTE_AS = 63199
LOCAL_AS = 63199
ENS192_MAC_200 = "00:50:56:af:97:a6"  # step.md Linux 200 ens192


def load_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        import os

        os.environ.setdefault(k.strip(), v.strip())


def api(method: str, path: str, body: dict | None = None, base: str = "http://10.133.151.200:8808") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        raise RuntimeError(f"{method} {path} -> {e.code}: {err}") from e


def ssh_bash(host: str, user: str, password: str, script: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=user, password=password, timeout=25, allow_agent=False, look_for_keys=False)
    i, o, e = c.exec_command("bash -se", timeout=120)
    i.write(script)
    i.channel.shutdown_write()
    out = o.read().decode(errors="replace") + e.read().decode(errors="replace")
    c.close()
    return out


def main() -> None:
    load_env()
    import os

    op_base = f"http://{os.environ.get('MTR_OP_HOST', '10.133.151.200')}:8808"

    print("=== 1. 开启 ARP 引流总开关 PUT /api/arp-spoof/settings ===")
    print(api("PUT", "/api/arp-spoof/settings", {"arp_spoof_enabled": True}, op_base))

    print("\n=== 2. 添加 ARP 条目 POST /api/arp-spoof/targets ===")
    arp_body = {
        "spoof_gateway_ip": SPOOF_IP,
        "satellite_vrf": SAT_VRF,
        "egress_iface": EGRESS,
        "enabled": True,
        "policy_mode": "gateway_only",
        "policy_cidrs": "",
        "note": "lab233-test",
    }
    arp_id = None
    try:
        arp = api("POST", "/api/arp-spoof/targets", arp_body, op_base)
        print(json.dumps(arp, ensure_ascii=False, indent=2))
        arp_id = arp.get("id")
    except RuntimeError as e:
        if "created_at" in str(e) or "500" in str(e):
            print("API 500 (created_at)，改用远端脚本插入:", e)
            script = (LAB / "remote-arp233-test.sh").read_text(encoding="utf-8")
            print(ssh_bash(os.environ.get("MTR_OP_HOST", "10.133.151.200"), "root", "1234qwer", script))
            rows = api("GET", "/api/arp-spoof/targets", None, op_base)
            for r in rows:
                if r.get("spoof_gateway_ip") == SPOOF_IP:
                    arp_id = r.get("id")
                    print("已有条目:", r)
        else:
            raise

    print("\n=== 3. 卫星 VRF reconcile POST /api/arp-spoof/satellite-vrfs/reconcile ===")
    print(json.dumps(api("POST", "/api/arp-spoof/satellite-vrfs/reconcile", {}, op_base), indent=2))

    print("\n=== 4. 可选 ipvlan reconcile POST /api/bgp/ipvlan-satellites/reconcile ===")
    try:
        print(json.dumps(api("POST", "/api/bgp/ipvlan-satellites/reconcile", {}, op_base), indent=2))
    except Exception as ex:
        print("ipvlan reconcile:", ex)

    time.sleep(3)

    print("\n=== 5. 200 本机：接口地址与 VRF ===")
    print(
        ssh_bash(
            "10.133.151.200",
            "root",
            "1234qwer",
            f"""
ip -br addr show {EGRESS} | head -1
ip addr show {EGRESS} | grep '{SPOOF_IP}' || echo 'no {SPOOF_IP} on {EGRESS}'
ip link show {SAT_VRF} 2>/dev/null | head -2 || echo 'no {SAT_VRF}'
ip -br addr show master {SAT_VRF} 2>/dev/null | head -5
""",
        )
    )

    print("\n=== 6. Linux 201：ping + ARP ===")
    out201 = ssh_bash(
        "10.133.151.201",
        "root",
        "1234qwer",
        f"""
ping -c 3 -W 1 {SPOOF_IP} || true
ip neigh show {SPOOF_IP} dev ens192 2>/dev/null || ip neigh show {SPOOF_IP} || true
""",
    )
    print(out201)
    mac_ok = ENS192_MAC_200.lower().replace(":", "") in out201.lower().replace(":", "")
    print(f"[{'PASS' if mac_ok else 'CHECK'}] 201 ARP MAC 是否为 200 ens192 ({ENS192_MAC_200}): {mac_ok}")

    print("\n=== 7. BGP 邻居 POST /api/bgp/neighbors（同 index.html addBgpNeighbor）===")
    bgp_body = {
        "vrf": SAT_VRF,
        "neighbor_ip": NEIGHBOR_201,
        "remote_as": REMOTE_AS,
        "role": "downstream",
        "source_ip": SPOOF_IP,
        "bgp_local_as": LOCAL_AS,
        "bgp_router_id": SPOOF_IP,
        "create_kernel_vrf_if_missing": True,
    }
    try:
        bgp = api("POST", "/api/bgp/neighbors", bgp_body, op_base)
        print(json.dumps(bgp, ensure_ascii=False, indent=2))
    except RuntimeError as e:
        if "409" in str(e) or "already" in str(e).lower():
            print("邻居已存在，尝试 PATCH:", e)
            bgp = api(
                "PATCH",
                f"/api/bgp/neighbors/{SAT_VRF}/{NEIGHBOR_201}",
                {
                    "remote_as": REMOTE_AS,
                    "source_ip": SPOOF_IP,
                    "role": "downstream",
                },
                op_base,
            )
            print(json.dumps(bgp, ensure_ascii=False, indent=2))
        else:
            raise

    time.sleep(5)

    print("\n=== 8. BGP 状态 GET /api/bgp/neighbors + Agent freeze ===")
    neighbors = api("GET", "/api/bgp/neighbors", None, op_base)
    hit = [n for n in neighbors if n.get("vrf") == SAT_VRF or SPOOF_IP in str(n)]
    print(json.dumps(hit, ensure_ascii=False, indent=2))

    agent = urllib.request.urlopen("http://10.133.151.200:9179/api/peers/freeze-status", timeout=30)
    freeze = json.loads(agent.read().decode())
    print(json.dumps(freeze, ensure_ascii=False, indent=2))

    print("\n=== 9. 200 Agent 邻居（vbgp* / 233）===")
    print(
        ssh_bash(
            "10.133.151.201",
            "root",
            "1234qwer",
            "curl -s http://127.0.0.1:9179/api/neighbors; echo; curl -s http://127.0.0.1:9179/api/peers/freeze-status",
        )
    )

    print("\n=== 10. 200 BGP TCP :179 ===")
    print(
        ssh_bash(
            "10.133.151.200",
            "root",
            "1234qwer",
            f"ss -tnp state established 2>/dev/null | grep -E '{SPOOF_IP}|{NEIGHBOR_201}' || ss -tnp | grep 179 | grep 233",
        )
    )

    print(f"\n完成。ARP 条目 id={arp_id} spoof={SPOOF_IP} vrf={SAT_VRF}")


if __name__ == "__main__":
    main()
