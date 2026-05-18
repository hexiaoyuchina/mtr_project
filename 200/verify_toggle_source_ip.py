#!/usr/bin/env python3
"""邻居启停后 TCP 源不得变为 0.0.0.0（应保留 SQLite meta）。"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

LAB = Path(__file__).resolve().parent
VRF = "vbgp10133153204"
PEER = "10.133.152.204"
EXPECT_SRC = "10.133.153.204"


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def http_json(method: str, url: str, body: dict | None = None) -> tuple[int, object]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw[:300]}


def find_row(neighbors: list) -> dict | None:
    for n in neighbors:
        if str(n.get("vrf")) == VRF and str(n.get("neighbor_ip")) == PEER:
            return n
    for n in neighbors:
        if str(n.get("neighbor_ip")) == PEER and str(n.get("source_ip") or "") == EXPECT_SRC:
            return n
    return None


def main() -> int:
    load_lab_env()
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    op = f"http://{host}:{os.environ.get('MTR_OP_PORT', '8808')}"

    _, neighbors = http_json("GET", f"{op}/api/bgp/neighbors")
    row = find_row(neighbors if isinstance(neighbors, list) else [])
    if not row:
        print("FAIL 未找到测试邻居")
        return 1
    vrf = str(row.get("vrf"))
    src0 = str(row.get("source_ip") or "")
    print(f"初始 source_ip={src0!r} enabled={row.get('enabled')}")

    # 关闭
    code, out = http_json(
        "POST",
        f"{op}/api/bgp/neighbors/{vrf}/{PEER}/toggle",
        {"enabled": False},
    )
    src_off = str((out or {}).get("source_ip") or "")
    print(f"关闭后 source_ip={src_off!r} http={code}")

    # 刷新列表
    _, neighbors2 = http_json("GET", f"{op}/api/bgp/neighbors")
    row2 = find_row(neighbors2 if isinstance(neighbors2, list) else [])
    src_list = str((row2 or {}).get("source_ip") or "")
    print(f"列表刷新 source_ip={src_list!r}")

    # 再打开
    code2, out2 = http_json(
        "POST",
        f"{op}/api/bgp/neighbors/{vrf}/{PEER}/toggle",
        {"enabled": True},
    )
    src_on = str((out2 or {}).get("source_ip") or "")
    print(f"开启后 source_ip={src_on!r} http={code2}")

    ok = all(
        s == EXPECT_SRC
        for s in (src_off, src_list, src_on)
        if s
    ) or (src_off == EXPECT_SRC and src_list == EXPECT_SRC)
    if src_off == EXPECT_SRC and src_list == EXPECT_SRC:
        print("RESULT: PASS")
        return 0
    print(f"RESULT: FAIL (期望 {EXPECT_SRC})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
