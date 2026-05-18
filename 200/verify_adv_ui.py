#!/usr/bin/env python3
"""验收：路由通告仅开关 + API body 仅 advertise_routes。"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

LAB = Path(__file__).resolve().parent


def load_lab_env() -> None:
    env_file = LAB / "lab.env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def http_json(method: str, url: str, body: dict | None = None, timeout: int = 30) -> tuple[int, object]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw[:300]}


def main() -> int:
    load_lab_env()
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    port = os.environ.get("MTR_OP_PORT", "8808")
    base = f"http://{host}:{port}"

    all_ok = True
    print(f"=== 路由通告验收 @ {base} ===\n")

    # 1) 静态页无旧 UI
    try:
        with urllib.request.urlopen(f"{base}/", timeout=20) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        all_ok &= check("GET static/index.html", False, str(e))
        html = ""
    if html:
        all_ok &= check("无 adv-from 输入", "adv-from" not in html)
        all_ok &= check("无 adv-apply 按钮", "adv-apply" not in html)
        all_ok &= check("有 adv-cap 开关文案", "adv-cap" in html)
        all_ok &= check("POST 仅 advertise_routes", 'advertise_routes: wantAdv ? 1 : 0' in html)
        all_ok &= check("表头 路由通告", "路由通告" in html)
        all_ok &= check("表头 路由入库", "路由入库" in html)
        all_ok &= check("BGP 路由页有查询按钮", "btnBrQuery" in html)
        all_ok &= check("BGP 路由页无立即同步", "btnBrSync" not in html)

    # 2) 邻居列表字段
    code, neighbors = http_json("GET", f"{base}/api/bgp/neighbors")
    all_ok &= check("GET /api/bgp/neighbors", code == 200, f"http {code}")
    if not isinstance(neighbors, list):
        all_ok &= check("neighbors 为数组", False)
        print("\n部分失败")
        return 1

    if not neighbors:
        all_ok &= check("存在至少一个邻居", False, "列表为空，跳过 API 开关测试")
    else:
        all_ok &= check("存在至少一个邻居", True, f"count={len(neighbors)}")
        n0 = neighbors[0]
        vrf = n0.get("vrf")
        nip = n0.get("neighbor_ip")
        has_adv_field = "advertise_routes" in n0
        all_ok &= check("邻居含 advertise_routes", has_adv_field, str(n0.get("advertise_routes")))
        # 列表不应再依赖 advertise_routes_from 展示
        if "advertise_routes_from" in n0:
            all_ok &= check("列表无 advertise_routes_from 字段", False, n0.get("advertise_routes_from"))
        else:
            all_ok &= check("列表无 advertise_routes_from 字段", True)

        # 3) 开关 API：先读状态，toggle off/on 仅 body advertise_routes
        path = f"/api/bgp/neighbors/{vrf}/{nip}/advertise"
        orig = int(n0.get("advertise_routes") or 0)
        for want in (0, 1):
            c2, out = http_json("POST", f"{base}{path}", {"advertise_routes": want})
            ok = c2 == 200 and isinstance(out, dict)
            all_ok &= check(f"POST advertise_routes={want}", ok, f"http {c2} vrf={vrf} peer={nip}")
            if ok:
                ar = int(out.get("advertise_routes", -1))
                all_ok &= check(f"  返回 advertise_routes={want}", ar == want, str(ar))

        # 恢复原状态
        http_json("POST", f"{base}{path}", {"advertise_routes": orig})

        c3, st = http_json("GET", f"{base}/api/bgp/neighbors/{vrf}/{nip}/advertise/status")
        all_ok &= check("GET advertise/status", c3 == 200 and isinstance(st, dict), f"status={st.get('status') if isinstance(st, dict) else st}")

        # 4) 拒绝带 advertise_routes_from 的旧 body（若后端严格）
        c4, _ = http_json(
            "POST",
            f"{base}{path}",
            {"advertise_routes": 0, "advertise_routes_from": "@downstream"},
        )
        # FastAPI 默认忽略额外字段；至少应成功且不把 from 写回列表
        all_ok &= check("带多余字段 POST 仍可用", c4 in (200, 422), f"http {c4}")

    print()
    if all_ok:
        print("=== 全部通过 ===")
        return 0
    print("=== 存在失败项 ===")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
