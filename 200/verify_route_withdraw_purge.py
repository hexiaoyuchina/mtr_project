#!/usr/bin/env python3
"""在 ROS RR 上增删路由，验证 Agent 入库在 withdraw / ingest reconcile 后清库。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
H200 = "10.133.151.200"
H210 = "10.133.151.210"
VRF = "gobgp-rr"
PEER = "10.133.153.204"
WINDOW = "upstream"
COMMENT = "lab-withdraw-purge"
BASE_NET = "100.66"
ROUTE_COUNT = 20
REMOVE_COUNT = 10


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def pw() -> str:
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


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


def rib_count(agent: str) -> int:
    q = urllib.parse.urlencode({"window": WINDOW, "vrf": VRF, "neighbor_ip": PEER})
    code, j = http_json("GET", f"{agent}/api/rib/routes/count?{q}")
    if code != 200:
        return -1
    return int((j or {}).get("count") or 0)


def has_prefix(op: str, prefix: str) -> bool:
    q = urllib.parse.urlencode(
        {
            "vrf": VRF,
            "neighbor_ip": PEER,
            "page": "1",
            "page_size": "500",
        }
    )
    code, j = http_json("GET", f"{op}/api/bgp/learned-routes?{q}", timeout=60)
    if code != 200:
        return False
    for r in (j or {}).get("routes") or []:
        if str(r.get("prefix")) == prefix:
            return True
    return False


def ros_import_rsc(rsc_body: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(H210, username="admin", password=pw(), timeout=25, allow_agent=False, look_for_keys=False)
    try:
        path = "/lab-withdraw-purge.rsc"
        sftp = c.open_sftp()
        with sftp.file(path, "w") as f:
            f.write(rsc_body)
        sftp.close()
        _, o, e = c.exec_command(f"/import file-name={path}", timeout=120)
        return (o.read() + e.read()).decode("utf-8", "replace")
    finally:
        c.close()


def build_rsc(add_count: int) -> str:
    lines = [
        f"/ip route remove [find comment={COMMENT}]",
        f"/routing bgp network remove [find comment={COMMENT}]",
    ]
    for i in range(1, add_count + 1):
        pfx = f"{BASE_NET}.{i}.0/24"
        lines.append(
            f"/ip route add dst-address={pfx} gateway=10.133.151.254 distance=1 comment={COMMENT}"
        )
        lines.append(
            f"/routing bgp network add network={pfx} synchronize=no comment={COMMENT}"
        )
    return "\n".join(lines) + "\n"


def build_remove_first_n_rsc(n: int) -> str:
    lines = []
    for i in range(1, n + 1):
        pfx = f"{BASE_NET}.{i}.0/24"
        lines.append(f"/ip route remove [find comment={COMMENT} and dst-address={pfx}]")
        lines.append(f"/routing bgp network remove [find comment={COMMENT} and network={pfx}]")
    return "\n".join(lines) + "\n"


def build_cleanup_rsc() -> str:
    return (
        f"/ip route remove [find comment={COMMENT}]\n"
        f"/routing bgp network remove [find comment={COMMENT}]\n"
    )


def ensure_store_on(op: str) -> bool:
    code, j = http_json(
        "POST",
        f"{op}/api/bgp/neighbors/{urllib.parse.quote(VRF)}/{PEER}/store-routes",
        {"store_received_routes": 1},
        timeout=600,
    )
    if code != 200:
        print(f"FAIL store ON http={code} {j}")
        return False
    print(f"  store ON: routes_cached={j.get('routes_cached')} pfx_rcd={j.get('routes_received')}")
    return True


def ingest(agent: str) -> tuple[int, int]:
    q = urllib.parse.urlencode({"window": WINDOW, "vrf": VRF, "neighbor_ip": PEER})
    code, j = http_json("POST", f"{agent}/api/rib/ingest-peer?{q}", timeout=600)
    if code != 200 or not isinstance(j, dict):
        print(f"  ingest http={code} {j}")
        return 0, 0
    ing = int(j.get("ingested") or 0)
    rem = int(j.get("removed") or 0)
    print(f"  ingest http={code} ingested={ing} removed={rem}")
    return ing, rem


def ensure_rr_up(agent: str) -> bool:
    code, j = http_json("GET", f"{agent}/api/rr/status", timeout=30)
    if code == 200 and isinstance(j, dict):
        if j.get("connected") is True:
            return True
        rx = j.get("rx_status") or {}
        if rx.get("rr_connected") is True:
            return True
        st = str(rx.get("rr_state") or j.get("state") or "").upper()
        if "ESTABLISHED" in st:
            return True
    http_json("POST", f"{agent}/api/rr/unfreeze", timeout=30)
    time.sleep(6)
    code, j = http_json("GET", f"{agent}/api/rr/status", timeout=30)
    if code != 200 or not isinstance(j, dict):
        return False
    if j.get("connected") is True:
        return True
    rx = j.get("rx_status") or {}
    return rx.get("rr_connected") is True or "ESTABLISHED" in str(rx.get("rr_state") or "").upper()


def main() -> int:
    load_lab_env()
    host = os.environ.get("MTR_OP_HOST", H200)
    op_port = os.environ.get("MTR_OP_PORT", "8808")
    op = f"http://{host}:{op_port}"
    agent = f"http://{host}:9179"

    print(f"=== 路由撤销清库验证 @ {host} ===\n")

    if not ensure_rr_up(agent):
        print("FAIL RR 未 Established，请先 python 200/unfreeze_rr_check.py 或 /api/rr/config")
        return 1
    print("[0] RR Established OK")

    cnt0 = rib_count(agent)
    print(f"[1] 基线 Agent RIB count={cnt0}")

    if not ensure_store_on(op):
        return 1

    print(f"\n[2] ROS 添加 {ROUTE_COUNT} 条测试路由 ({BASE_NET}.x) …")
    out = ros_import_rsc(build_rsc(ROUTE_COUNT))
    if "failure" in out.lower() and "error" in out.lower():
        print(out)
    else:
        print("  import OK")
    time.sleep(12)

    ing1, _ = ingest(agent)
    cnt_after_add = rib_count(agent)
    test_present = f"{BASE_NET}.10.0/24"
    test_gone_later = f"{BASE_NET}.5.0/24"
    test_stay = f"{BASE_NET}.15.0/24"
    print(f"  count after add+ingest={cnt_after_add} (delta={cnt_after_add - cnt0})")
    if not has_prefix(op, test_present):
        print(f"WARN 列表中未见 {test_present}，继续观察 withdraw")

    print(f"\n[3] ROS 撤销前 {REMOVE_COUNT} 条 …")
    ros_import_rsc(build_remove_first_n_rsc(REMOVE_COUNT))
    time.sleep(15)
    cnt_after_withdraw = rib_count(agent)
    drop_watch = cnt_after_add - cnt_after_withdraw
    gone_watch = not has_prefix(op, test_gone_later)
    stay_watch = has_prefix(op, test_stay)
    print(f"  count after withdraw wait={cnt_after_withdraw} (watch 减少 {drop_watch})")
    print(f"  {test_gone_later} 已删除={gone_watch}, {test_stay} 仍在={stay_watch}")

    print("\n[4] 再次 ingest（reconcile 应 removed>0）…")
    _, removed = ingest(agent)
    cnt_final = rib_count(agent)
    drop_total = cnt_after_add - cnt_final
    gone_final = not has_prefix(op, test_gone_later)
    print(f"  count final={cnt_final} (相对 add 后减少 {drop_total})")

    print("\n[5] ROS 清理测试路由 …")
    ros_import_rsc(build_cleanup_rsc())
    time.sleep(3)

    ok_watch = drop_watch >= REMOVE_COUNT - 2 or gone_watch
    ok_reconcile = removed >= REMOVE_COUNT - 2 or drop_total >= REMOVE_COUNT - 2 or gone_final
    print("\n=== 汇总 ===")
    print(f"  增加后 count: {cnt_after_add}")
    print(f"  withdraw 后 count: {cnt_after_withdraw} (期望约减 {REMOVE_COUNT})")
    print(f"  ingest removed: {removed}")
    print(f"  最终 count: {cnt_final}")
    if ok_watch or ok_reconcile:
        print("RESULT: PASS")
        return 0
    print("RESULT: FAIL (撤销后库中条数/前缀未减少)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
