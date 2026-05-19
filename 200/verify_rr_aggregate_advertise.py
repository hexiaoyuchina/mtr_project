#!/usr/bin/env python3
"""验证 gobgp-rr / 153.204：RR 行通告汇总 source_ip=153.204 的下游库 → RX→ROS。"""
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
RR_VRF = "gobgp-rr"
RR_PEER = "10.133.153.204"
SPOOF_SOURCE = "10.133.153.204"
DOWNSTREAM_PEER = "10.133.152.204"
TARGET_VRF_CANDIDATES = ("vbgp10133153204", "vbgp10133152204")
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


def rib_count(agent: str, window: str, vrf: str, nip: str) -> int:
    q = urllib.parse.urlencode({"window": window, "vrf": vrf, "neighbor_ip": nip})
    code, j = http_json("GET", f"{agent}/api/rib/routes/count?{q}")
    return int((j or {}).get("count") or 0) if code == 200 else -1


def find_downstream_vrf(neighbors: list) -> str | None:
    for vrf in TARGET_VRF_CANDIDATES:
        for n in neighbors:
            if str(n.get("vrf")) == vrf and str(n.get("neighbor_ip")) == DOWNSTREAM_PEER:
                return vrf
    for n in neighbors:
        if str(n.get("neighbor_ip")) == DOWNSTREAM_PEER and str(n.get("source_ip") or "") == SPOOF_SOURCE:
            return str(n.get("vrf"))
    return None


def find_rr(neighbors: list) -> dict | None:
    for n in neighbors:
        if str(n.get("vrf")) == RR_VRF and str(n.get("neighbor_ip")) == RR_PEER:
            return n
    return None


def neighbor_row(op: str, ds_vrf: str) -> dict | None:
    code, neighbors = http_json("GET", f"{op}/api/bgp/neighbors")
    if code != 200 or not isinstance(neighbors, list):
        return None
    for n in neighbors:
        if str(n.get("vrf")) == ds_vrf and str(n.get("neighbor_ip")) == DOWNSTREAM_PEER:
            return n
    return None


def ensure_downstream_rib(op: str, agent: str, ds_vrf: str) -> int:
    """下游库为空时解冻、等待学路由、打开入库并 ingest。"""
    cnt = rib_count(agent, "downstream", ds_vrf, DOWNSTREAM_PEER)
    if cnt >= EXPECT_MIN:
        return cnt
    print("[prep] 解冻 gobgp …")
    http_json("POST", f"{op}/api/gobgp/unfreeze", {}, timeout=60)
    t0 = time.time()
    while time.time() - t0 < 120:
        row = neighbor_row(op, ds_vrf) or {}
        rx = int(row.get("routes_received") or 0)
        st = str(row.get("session_state") or "")
        if rx >= EXPECT_MIN and st == "Established":
            print(f"     下游已学 {rx} 条 session={st}")
            break
        time.sleep(3)
    print(f"[prep] 下游库仅 {cnt} 条，打开 store + ingest …")
    path = f"/api/bgp/neighbors/{urllib.parse.quote(ds_vrf)}/{DOWNSTREAM_PEER}/store-routes"
    code, j = http_json(
        "POST",
        f"{op}{path}",
        {"store_received_routes": 1},
        timeout=600,
    )
    if code != 200:
        print(f"WARN store-routes http={code} {j}")
    else:
        print(f"     store-routes: routes_cached={((j or {}) if isinstance(j, dict) else {}).get('routes_cached')}")
    time.sleep(2)
    return rib_count(agent, "downstream", ds_vrf, DOWNSTREAM_PEER)


def agent_rr_aggregate_smoke(agent: str, min_routes: int) -> bool:
    """Agent 直连：src_peers 聚合 + RX（实验室下游库为空时用 upstream 冒烟）。"""
    task_id = f"verify-rr-agg-smoke-{int(time.time())}"
    body = {
        "task_id": task_id,
        "src_peers": [
            {"window": "upstream", "vrf": RR_VRF, "neighbor_ip": RR_PEER},
        ],
        "target": "rr",
        "enable": True,
        "batch_size": 5000,
    }
    code, j = http_json("POST", f"{agent}/api/rib/advertise", body, timeout=120)
    if code != 200:
        print(f"FAIL agent smoke start http={code} {j}")
        return False
    t0 = time.time()
    while time.time() - t0 < 300:
        code2, st = http_json("GET", f"{agent}/api/rib/advertise/status?task_id={task_id}")
        if code2 != 200:
            time.sleep(2)
            continue
        status = str((st or {}).get("status") or "")
        added = int((st or {}).get("added") or 0)
        msg = str((st or {}).get("message") or "")
        if added >= min_routes and "aggregate" in msg.lower():
            print(f"[smoke] agent aggregate added>={min_routes}，触发撤销 …")
            http_json("POST", f"{agent}/api/rib/withdraw", {**body, "enable": False}, timeout=120)
            return True
        if status in {"completed", "error"}:
            print(f"[smoke] agent aggregate status={status} added={added} msg={msg[:120]}")
            ok = status == "completed" and added >= min_routes and "aggregate" in msg.lower()
            if ok:
                http_json("POST", f"{agent}/api/rib/withdraw", {**body, "enable": False}, timeout=120)
            return ok
        time.sleep(2)
    print("FAIL agent smoke timeout")
    return False


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

    print(f"=== RR 聚合路由通告验证 @ {host} ===\n")

    code, neighbors = http_json("GET", f"{op}/api/bgp/neighbors")
    if code != 200 or not isinstance(neighbors, list):
        print(f"FAIL neighbors http={code}")
        return 1

    rr = find_rr(neighbors)
    if not rr:
        print(f"FAIL 未找到 RR 行 {RR_VRF}/{RR_PEER}")
        return 1
    ds_vrf = find_downstream_vrf(neighbors)
    if not ds_vrf:
        print("FAIL 未找到下游行 (152.204)")
        return 1

    print(f"[rr]   vrf={RR_VRF} neighbor={RR_PEER}")
    print(f"[down] vrf={ds_vrf} neighbor={DOWNSTREAM_PEER}")

    upstream_cnt = rib_count(agent, "upstream", RR_VRF, RR_PEER)
    downstream_cnt = ensure_downstream_rib(op, agent, ds_vrf)
    print(f"[rib] upstream/{RR_VRF}/{RR_PEER}={upstream_cnt} (RR 通告不应再读此库)")
    print(f"[rib] downstream/{ds_vrf}/{DOWNSTREAM_PEER}={downstream_cnt} (RR 聚合来源)")
    lab_no_downstream = downstream_cnt < EXPECT_MIN
    if lab_no_downstream:
        print(
            f"WARN 下游库仅 {downstream_cnt} 条（201 未向 200 回传），"
            f"将验证 OP 不误读 upstream + Agent 聚合 RX 冒烟"
        )

    agg_expect = downstream_cnt if downstream_cnt > 0 else EXPECT_MIN
    sent0 = int(rr.get("routes_sent") or 0)

    if int(rr.get("advertise_routes") or 0):
        http_json(
            "POST",
            f"{op}/api/bgp/neighbors/{urllib.parse.quote(RR_VRF)}/{RR_PEER}/advertise",
            {"advertise_routes": 0},
            timeout=300,
        )
        wait_advertise(op, RR_VRF, RR_PEER)
        time.sleep(2)

    print("\n[ON] RR 行打开路由通告（聚合下游）…")
    code, j = http_json(
        "POST",
        f"{op}/api/bgp/neighbors/{urllib.parse.quote(RR_VRF)}/{RR_PEER}/advertise",
        {"advertise_routes": 1},
    )
    if code != 200:
        print(f"FAIL advertise ON http={code} {j}")
        return 1
    st = wait_advertise(op, RR_VRF, RR_PEER)
    print(f"     task: {st}")
    msg = str(st.get("message") or "")
    if "聚合" not in msg and "aggregate" not in msg.lower() and "下游" not in msg:
        print(f"WARN 任务消息未体现聚合: {msg[:200]}")

    _, neighbors2 = http_json("GET", f"{op}/api/bgp/neighbors")
    rr2 = find_rr(neighbors2 if isinstance(neighbors2, list) else [])
    sent1 = int((rr2 or {}).get("routes_sent") or 0)
    added = int(st.get("added") or 0)
    total = int(st.get("total_routes") or 0)
    min_expect = min(EXPECT_MIN, agg_expect) if agg_expect > 0 else EXPECT_MIN
    ok_on = (
        st.get("status") == "completed"
        and total >= min_expect
        and added >= min_expect
        and ("聚合" in msg or "下游" in msg or "aggregate" in msg.lower())
    )
    print(
        f"     added={added} total_routes={total} downstream_cnt={downstream_cnt} "
        f"routes_sent {sent0} -> {sent1}"
    )

    print("\n[OFF] RR 行关闭路由通告 …")
    http_json(
        "POST",
        f"{op}/api/bgp/neighbors/{urllib.parse.quote(RR_VRF)}/{RR_PEER}/advertise",
        {"advertise_routes": 0},
        timeout=300,
    )
    st2 = wait_advertise(op, RR_VRF, RR_PEER, timeout_s=180)
    print(f"     task: {st2}")
    ok_off = st2.get("status") == "completed"

    op_empty_ok = False
    if lab_no_downstream:
        op_empty_ok = (
            st.get("status") == "completed"
            and int(st.get("added") or 0) == 0
            and "下游" in msg
            and upstream_cnt > 0
        )
        print(f"\n[check] OP 未误读 upstream 库: op_empty_ok={op_empty_ok}")
        smoke_min = min(EXPECT_MIN, max(upstream_cnt, 1))
        smoke_ok = agent_rr_aggregate_smoke(agent, smoke_min)
        print(f"[check] Agent src_peers 聚合 RX: smoke_ok={smoke_ok}")
        if op_empty_ok and smoke_ok and ok_off:
            print("\n=== 汇总 ===")
            print("RESULT: PASS (实验室无下游入库；OP 拒绝 + Agent 聚合冒烟通过)")
            print("NOTE: 201→200 下游 adj-in 有路由后，下游库>0 时将走完整 RR 聚合 E2E")
            return 0

    print("\n=== 汇总 ===")
    if ok_on and ok_off:
        print("RESULT: PASS")
        return 0
    if not ok_on:
        print("RESULT: FAIL (RR 聚合通告未完成或条数不足)")
    if not ok_off:
        print("RESULT: FAIL (关闭通告异常)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
