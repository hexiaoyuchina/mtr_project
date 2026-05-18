#!/usr/bin/env python3
"""验证 gobgp-rr / 10.133.153.204 路由入库开关与持久库条数。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LAB = Path(__file__).resolve().parent
VRF = "gobgp-rr"
PEER = "10.133.153.204"
WINDOW = "upstream"
EXPECT_MIN = 90  # 允许少量波动


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


def http_json(method: str, url: str, body: dict | None = None, timeout: int = 120) -> tuple[int, object]:
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
            return e.code, {"detail": raw[:500]}


def find_neighbor(neighbors: list, vrf: str, ip: str) -> dict | None:
    for n in neighbors:
        if str(n.get("vrf")) == vrf and str(n.get("neighbor_ip")) == ip:
            return n
    return None


def rib_count(agent_base: str) -> int:
    q = urllib.parse.urlencode({"window": WINDOW, "vrf": VRF, "neighbor_ip": PEER})
    code, j = http_json("GET", f"{agent_base}/api/rib/routes/count?{q}")
    if code != 200:
        return -1
    return int((j or {}).get("count") or 0)


def rib_policy(agent_base: str) -> dict:
    q = urllib.parse.urlencode({"vrf": VRF, "neighbor_ip": PEER})
    code, j = http_json("GET", f"{agent_base}/api/rib/policy?{q}")
    return {"http": code, "body": j}


def main() -> int:
    load_lab_env()
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    op_port = os.environ.get("MTR_OP_PORT", "8808")
    op = f"http://{host}:{op_port}"
    agent = f"http://{host}:9179"

    print(f"=== 路由入库验证 @ {host} ===")
    print(f"peer: vrf={VRF} neighbor={PEER} window={WINDOW}\n")

    code, neighbors = http_json("GET", f"{op}/api/bgp/neighbors")
    if code != 200 or not isinstance(neighbors, list):
        print(f"FAIL GET neighbors http={code} {neighbors}")
        return 1
    row = find_neighbor(neighbors, VRF, PEER)
    if not row:
        print("FAIL 未找到 gobgp-rr / 10.133.153.204 邻居行")
        return 1

    pfx = int(row.get("routes_received") or 0)
    cached0 = int(row.get("routes_cached") or 0)
    store0 = int(row.get("store_received_routes") or 0)
    print(f"[1] 初始: session pfx_rcd={pfx} routes_cached={cached0} store={store0}")
    print(f"    Agent rib count={rib_count(agent)} policy={rib_policy(agent)}")

    # 先关再开，保证可观测 ingest
    if store0:
        code, j = http_json(
            "POST",
            f"{op}/api/bgp/neighbors/{urllib.parse.quote(VRF)}/{PEER}/store-routes",
            {"store_received_routes": 0},
        )
        print(f"[2] 预置关闭 store http={code}")
        time.sleep(1)

    print("[3] 打开路由入库 …")
    code, j = http_json(
        "POST",
        f"{op}/api/bgp/neighbors/{urllib.parse.quote(VRF)}/{PEER}/store-routes",
        {"store_received_routes": 1},
        timeout=600,
    )
    if code != 200:
        print(f"FAIL store ON http={code} {j}")
        return 1
    cached_on = int(j.get("routes_cached") or 0)
    pfx_on = int(j.get("routes_received") or 0)
    cnt_on = rib_count(agent)
    print(f"    OP 返回: pfx_rcd={pfx_on} routes_cached={cached_on}")
    print(f"    Agent rib count={cnt_on} policy={rib_policy(agent)}")

    ok_on = cnt_on >= EXPECT_MIN or cached_on >= EXPECT_MIN
    if pfx_on < EXPECT_MIN:
        print(f"WARN 会话仅收到 {pfx_on} 条，RR 侧可能未通告满 100 条")
    if not ok_on:
        print(f"FAIL 入库后持久库条数不足 (count={cnt_on}, cached={cached_on}, 期望>={EXPECT_MIN})")
        # 再试一次 ingest
        q = urllib.parse.urlencode({"window": WINDOW, "vrf": VRF, "neighbor_ip": PEER})
        code2, ing = http_json("POST", f"{op}/api/bgp/learned-routes/ingest?{q}", timeout=600)
        print(f"    补试 ingest http={code2} {ing}")
        cnt_on = rib_count(agent)
        _, nb2 = http_json("GET", f"{op}/api/bgp/neighbors")
        row2 = find_neighbor(nb2, VRF, PEER) if isinstance(nb2, list) else None
        cached_on = int((row2 or {}).get("routes_cached") or 0)
        ok_on = cnt_on >= EXPECT_MIN or cached_on >= EXPECT_MIN

    qs = urllib.parse.urlencode(
        {"vrf": VRF, "neighbor_ip": PEER, "page": "1", "page_size": "5"}
    )
    code, lr = http_json("GET", f"{op}/api/bgp/learned-routes?{qs}")
    total = int((lr or {}).get("total") or 0) if code == 200 else -1
    sample = (lr or {}).get("routes") or []
    print(f"    OP learned-routes total={total} sample_prefixes={[r.get('prefix') for r in sample[:3]]}")

    print("[4] 关闭路由入库 …")
    code, j = http_json(
        "POST",
        f"{op}/api/bgp/neighbors/{urllib.parse.quote(VRF)}/{PEER}/store-routes",
        {"store_received_routes": 0},
    )
    if code != 200:
        print(f"FAIL store OFF http={code} {j}")
        return 1
    cnt_off = rib_count(agent)
    pol = rib_policy(agent)
    store_off = bool((pol.get("body") or {}).get("store_routes"))
    print(f"    Agent rib count={cnt_off} (关闭后应保留已有数据) store_routes={store_off}")

    print("[5] 关闭后再次 ingest（应拒绝或 ingested=0）…")
    q = urllib.parse.urlencode({"window": WINDOW, "vrf": VRF, "neighbor_ip": PEER})
    code, ing = http_json("POST", f"{agent}/api/rib/ingest-peer?{q}", timeout=120)
    ingested = int((ing or {}).get("ingested") or 0) if isinstance(ing, dict) else 0
    msg = (ing or {}).get("message", "") if isinstance(ing, dict) else str(ing)
    print(f"    ingest-peer http={code} ingested={ingested} message={msg!r}")
    cnt_after = rib_count(agent)
    ok_off = not store_off and (ingested == 0 or "disabled" in str(msg).lower() or code != 200)
    print(f"    count after blocked ingest={cnt_after}")

    print("\n=== 汇总 ===")
    print(f"  会话收到: {pfx_on}")
    print(f"  入库开启后持久库: {cnt_on} (OP cached={cached_on}, list total={total})")
    print(f"  入库关闭 policy.store_routes={store_off}, 强制 ingest ingested={ingested}")
    if ok_on and ok_off:
        print("RESULT: PASS")
        return 0
    if not ok_on:
        print("RESULT: FAIL (入库条数不足)")
    if not ok_off:
        print("RESULT: FAIL (关闭后仍可灌库或 policy 未关)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
