#!/usr/bin/env python3
"""验证下游 vbgp* / 152.204：路由通告从 153.204 入库来源读出并推送。"""
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
TARGET_VRF_CANDIDATES = (
    "vbgp10133153204",  # TCP 源 10.133.153.204 去点
    "vbgp10133152204",
)
TARGET_PEER = "10.133.152.204"
SOURCE_RR = "10.133.153.204"
RR_VRF = "gobgp-rr"
EXPECT_MIN = 90


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def http_json(method: str, url: str, body: dict | None = None, timeout: int = 180) -> tuple[int, object]:
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
            return e.code, {"detail": raw[:400]}


def find_downstream(neighbors: list) -> tuple[str, dict] | None:
    for vrf in TARGET_VRF_CANDIDATES:
        for n in neighbors:
            if str(n.get("vrf")) == vrf and str(n.get("neighbor_ip")) == TARGET_PEER:
                return vrf, n
    for n in neighbors:
        if str(n.get("neighbor_ip")) != TARGET_PEER:
            continue
        if str(n.get("source_ip") or "") == SOURCE_RR:
            return str(n.get("vrf")), n
    return None


def rib_count(agent: str, window: str, vrf: str, nip: str) -> int:
    q = urllib.parse.urlencode({"window": window, "vrf": vrf, "neighbor_ip": nip})
    code, j = http_json("GET", f"{agent}/api/rib/routes/count?{q}")
    return int((j or {}).get("count") or 0) if code == 200 else -1


def wait_advertise(op: str, vrf: str, peer: str, timeout_s: int = 300) -> dict:
    path = f"/api/bgp/neighbors/{urllib.parse.quote(vrf)}/{peer}/advertise/status"
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        code, st = http_json("GET", f"{op}{path}")
        if code == 200 and isinstance(st, dict):
            if st.get("status") in {"completed", "error", "idle"}:
                return st
        time.sleep(2)
    return {"status": "timeout"}


def main() -> int:
    load_lab_env()
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    op = f"http://{host}:{os.environ.get('MTR_OP_PORT', '8808')}"
    agent = f"http://{host}:9179"

    print(f"=== 下游路由通告验证 @ {host} ===\n")

    code, neighbors = http_json("GET", f"{op}/api/bgp/neighbors")
    if code != 200 or not isinstance(neighbors, list):
        print(f"FAIL neighbors http={code}")
        return 1
    found = find_downstream(neighbors)
    if not found:
        print("FAIL 未找到下游行 (152.204 + TCP源153.204)")
        for n in neighbors:
            if TARGET_PEER in str(n.get("neighbor_ip")):
                print(" ", n)
        return 1
    vrf, row = found
    print(f"[peer] vrf={vrf} neighbor={TARGET_PEER} source={row.get('source_ip')}")
    print(f"       rx/tx={row.get('routes_received')}/{row.get('routes_sent')} cached={row.get('routes_cached')}")

    src_cnt = rib_count(agent, "upstream", RR_VRF, SOURCE_RR)
    dst_cnt = rib_count(agent, "downstream", vrf, TARGET_PEER)
    print(f"[rib] 来源 {RR_VRF}/{SOURCE_RR} upstream={src_cnt}")
    print(f"[rib] 目标 {vrf}/{TARGET_PEER} downstream={dst_cnt}")
    if src_cnt < EXPECT_MIN:
        print(f"WARN 来源不足 {EXPECT_MIN}，请先在 RR 打开路由入库")

    sent0 = int(row.get("routes_sent") or 0)

    if int(row.get("advertise_routes") or 0):
        http_json(
            "POST",
            f"{op}/api/bgp/neighbors/{urllib.parse.quote(vrf)}/{TARGET_PEER}/advertise",
            {"advertise_routes": 0},
            timeout=300,
        )
        wait_advertise(op, vrf, TARGET_PEER)
        time.sleep(2)

    print("\n[ON] 打开路由通告 …")
    code, j = http_json(
        "POST",
        f"{op}/api/bgp/neighbors/{urllib.parse.quote(vrf)}/{TARGET_PEER}/advertise",
        {"advertise_routes": 1},
    )
    if code != 200:
        print(f"FAIL advertise ON http={code} {j}")
        return 1
    st = wait_advertise(op, vrf, TARGET_PEER)
    print(f"     task: {st}")

    _, neighbors2 = http_json("GET", f"{op}/api/bgp/neighbors")
    row2 = None
    for n in neighbors2 if isinstance(neighbors2, list) else []:
        if str(n.get("vrf")) == vrf and str(n.get("neighbor_ip")) == TARGET_PEER:
            row2 = n
    sent1 = int((row2 or {}).get("routes_sent") or 0)
    added = int(st.get("added") or 0)
    total = int(st.get("total_routes") or 0)
    min_expect = min(EXPECT_MIN, src_cnt) if src_cnt > 0 else EXPECT_MIN
    ratio_ok = src_cnt > 0 and sent1 >= int(src_cnt * 0.9)
    ok_on = (
        st.get("status") == "completed"
        and total >= min_expect
        and (added >= min_expect or sent1 >= min_expect)
        and (ratio_ok or sent1 >= EXPECT_MIN)
        and sent1 > sent0
    )
    print(
        f"     added={added} total_routes={total} src_cnt={src_cnt} "
        f"routes_sent {sent0} -> {sent1} ratio_ok={ratio_ok}"
    )

    print("\n[OFF] 关闭路由通告 …")
    code, j = http_json(
        "POST",
        f"{op}/api/bgp/neighbors/{urllib.parse.quote(vrf)}/{TARGET_PEER}/advertise",
        {"advertise_routes": 0},
        timeout=300,
    )
    if code != 200:
        print(f"FAIL advertise OFF http={code} {j}")
        return 1
    st2 = wait_advertise(op, vrf, TARGET_PEER, timeout_s=180)
    print(f"     task: {st2}")
    time.sleep(3)
    _, neighbors3 = http_json("GET", f"{op}/api/bgp/neighbors")
    row3 = None
    for n in neighbors3 if isinstance(neighbors3, list) else []:
        if str(n.get("vrf")) == vrf and str(n.get("neighbor_ip")) == TARGET_PEER:
            row3 = n
    sent2 = int((row3 or {}).get("routes_sent") or 0)
    withdrawn = int(st2.get("added") or 0)  # withdraw uses same field name? check - message says withdrawn
    msg2 = str(st2.get("message") or "")
    ok_off = int((row3 or {}).get("advertise_routes") or 0) == 0
    print(f"     routes_sent {sent1} -> {sent2} advertise_routes=0:{ok_off}")
    print(f"     message: {msg2}")

    print("\n=== 汇总 ===")
    if ok_on and ok_off:
        print("RESULT: PASS")
        return 0
    if not ok_on:
        print("RESULT: FAIL (开启后未从来源通告足够路由)")
    if not ok_off:
        print("RESULT: FAIL (关闭后状态异常)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
