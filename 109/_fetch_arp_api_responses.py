#!/usr/bin/env python3
"""Fetch OP API responses used by ARP 引流 page (109)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import paramiko

DEPLOY_DIR = Path(__file__).resolve().parent

ENDPOINTS = [
    ("GET", "/health"),
    ("GET", "/api/arp-spoof/settings"),
    ("GET", "/api/arp-spoof/targets"),
    ("GET", "/api/host-ifaces"),
    ("GET", "/api/bgp/satellite-vrfs"),
    ("GET", "/api/bgp/vrfs"),
    ("GET", "/api/bgp/neighbor-form-hints"),
    ("GET", "/api/bgp/neighbors"),
    ("GET", "/api/global"),
]


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY_DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()
        if name == "env":
            break


def main() -> None:
    load_env()
    port = os.environ.get("MTR_OP_PORT", "8808").strip()
    base = f"http://127.0.0.1:{port}"

    py = f"""
import json, urllib.request
base = {base!r}
eps = {json.dumps(ENDPOINTS)}
out = {{}}
for method, path in eps:
    url = base + path
    try:
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode('utf-8', errors='replace')
            try:
                out[path] = {{'status': r.status, 'body': json.loads(body)}}
            except json.JSONDecodeError:
                out[path] = {{'status': r.status, 'body_raw': body[:2000]}}
    except Exception as e:
        out[path] = {{'error': str(e)}}
print(json.dumps(out, ensure_ascii=False, indent=2))
"""

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ["MTR_OP_HOST"],
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    _, stdout, stderr = c.exec_command(f"python3 <<'PY'\n{py}\nPY", timeout=120)
    raw = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if err.strip():
        print("STDERR:", err)
    out_path = DEPLOY_DIR / "_arp_api_snapshot.json"
    try:
        data = json.loads(raw)
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved {out_path}")
    except json.JSONDecodeError:
        print(raw)
        return
    c.close()

    # human summary
    for path, item in data.items():
        print("\n" + "=" * 72)
        print(path)
        print("=" * 72)
        if "error" in item:
            print("ERROR:", item["error"])
            continue
        body = item.get("body")
        if path == "/api/arp-spoof/targets" and isinstance(body, list):
            print(f"数组长度: {len(body)}")
            enabled = sum(1 for x in body if x.get("enabled"))
            print(f"enabled=true: {enabled}")
            print("\n--- 表格字段说明 (ArpTargetOut) ---")
            print("id, enabled, spoof_gateway_ip, satellite_vrf, egress_iface,")
            print("policy_mode, policy_cidrs, note, created_at")
            print("\n--- 245/247/249 ---")
            for x in body:
                if x.get("spoof_gateway_ip", "").endswith((".245", ".247", ".249")):
                    print(json.dumps(x, ensure_ascii=False))
            print("\n--- 全部条目 ---")
            print(json.dumps(body, ensure_ascii=False, indent=2))
        elif path == "/api/bgp/neighbors" and isinstance(body, dict):
            print(json.dumps(body, ensure_ascii=False, indent=2))
        elif path == "/api/bgp/vrfs" and isinstance(body, list):
            vbgp = [x for x in body if str(x.get("vrf", "")).startswith("vbgp")]
            print(f"VRF总数: {len(body)}, vbgp*: {len(vbgp)}")
            print(json.dumps(body, ensure_ascii=False, indent=2))
        elif path == "/api/host-ifaces":
            ifaces = (body or {}).get("ifaces") or []
            print(f"接口数: {len(ifaces)}")
            for i in ifaces:
                if "eno1" in i.get("name", "") or "enp59" in i.get("name", ""):
                    print(json.dumps(i, ensure_ascii=False))
            print("(完整列表见 JSON 文件)")
        else:
            print(json.dumps(body, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
