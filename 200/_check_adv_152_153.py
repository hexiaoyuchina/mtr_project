#!/usr/bin/env python3
"""查询实验室：152.204 / 153.204 邻居与路由通告开关状态。"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

LAB = Path(__file__).resolve().parent
PEERS = ("10.133.152.204", "10.133.153.204")


def load_lab_env() -> None:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def get_json(url: str, timeout: int = 30) -> object:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}


def main() -> int:
    load_lab_env()
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200")
    port = os.environ.get("MTR_OP_PORT", "8808")
    op = f"http://{host}:{port}"
    agent = f"http://{host}:9179"

    print(f"OP={op}  agent={agent}\n")

    nb = get_json(f"{op}/api/bgp/neighbors")
    if not isinstance(nb, list):
        print("FAIL: neighbors not a list", nb)
        return 1

    print("=== 邻居行（152.204 / 153.204）===")
    matched = []
    for n in nb:
        nip = str(n.get("neighbor_ip") or n.get("address") or "")
        if nip not in PEERS:
            continue
        matched.append(n)
        row = {
            "vrf": n.get("vrf"),
            "neighbor_ip": nip,
            "role": n.get("role"),
            "source_ip": n.get("source_ip"),
            "state": n.get("state"),
            "enabled": n.get("enabled"),
            "ingest_routes": n.get("ingest_routes"),
            "advertise_routes": n.get("advertise_routes"),
            "routes_received": n.get("routes_received"),
            "routes_sent": n.get("routes_sent"),
            "routes_cached": n.get("routes_cached"),
            "pfx_rcd": n.get("pfx_rcd"),
            "pfx_adv": n.get("pfx_adv"),
        }
        print(json.dumps(row, ensure_ascii=False))

    for nip in PEERS:
        row = next((x for x in matched if str(x.get("neighbor_ip")) == nip), None)
        if not row:
            print(f"\nWARN: 未找到邻居 {nip}")
            continue
        vrf = str(row.get("vrf"))
        st = get_json(
            f"{op}/api/bgp/neighbors/{urllib.parse.quote(vrf)}/{nip}/advertise/status"
        )
        print(f"\n=== advertise/status {vrf}/{nip} ===")
        print(json.dumps(st, ensure_ascii=False))

    print("\n=== RIB 持久库条数 ===")
    for window, vrf, nip in (
        ("upstream", "gobgp-rr", "10.133.153.204"),
        ("downstream", "vbgp10133153204", "10.133.152.204"),
    ):
        q = urllib.parse.urlencode({"window": window, "vrf": vrf, "neighbor_ip": nip})
        try:
            c = get_json(f"{agent}/api/rib/routes/count?{q}")
            print(f"  {window} {vrf}/{nip} -> {c.get('count')}")
        except Exception as e:
            print(f"  {window} {vrf}/{nip} -> ERR {e}")

    ds = next((x for x in matched if str(x.get("neighbor_ip")) == "10.133.152.204"), None)
    rr = next((x for x in matched if str(x.get("neighbor_ip")) == "10.133.153.204"), None)
    print("\n=== 结论（下游 152.204 行）===")
    if not ds:
        print("未配置下游邻居 10.133.152.204")
        return 1
    adv = int(ds.get("advertise_routes") or 0)
    sent = int(ds.get("routes_sent") or 0)
    src = str(ds.get("source_ip") or "")
    print(f"  路由通告开关 advertise_routes={adv} ({'已打开' if adv else '未打开'})")
    print(f"  TCP 源 source_ip={src!r}（期望 10.133.153.204 表示从 RR 库读出通告给 152.204）")
    print(f"  已向对端发送 routes_sent={sent} / pfx_adv={ds.get('pfx_adv')}")
    print(f"  BGP 状态 state={ds.get('state')} enabled={ds.get('enabled')}")
    if adv and sent > 0:
        print("  → 通告已开且会话侧有发送计数，152.204 应已收到来自控制面的路由通告。")
    elif adv and sent == 0:
        print("  → 通告开关已开但 routes_sent=0，可能任务未完成、来源库为空或会话未 Established。")
    else:
        print("  → 通告开关未开，不会向 152.204 推送持久库路由。")

    if rr:
        print("\n=== RR 行 153.204（聚合通告给 ROS）===")
        print(
            f"  advertise_routes={rr.get('advertise_routes')} "
            f"routes_sent={rr.get('routes_sent')} state={rr.get('state')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
