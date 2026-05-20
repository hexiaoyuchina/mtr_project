#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import paramiko

DEPLOY = Path(__file__).resolve().parent


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()
        break


def curl_save(c: paramiko.SSHClient, url: str, path: str) -> bool:
    code, out, err = _run(c, f'curl -sf -m 30 "{url}" -o {path} && wc -c < {path}')
    if code != 0:
        print(f"curl fail {url}: {err or out}")
        return False
    return True


def _run(c: paramiko.SSHClient, cmd: str) -> tuple[int, str, str]:
    i, o, e = c.exec_command(cmd, timeout=120)
    return o.channel.recv_exit_status(), o.read().decode(errors="replace"), e.read().decode(errors="replace")


def cat_json(c: paramiko.SSHClient, path: str) -> object:
    code, out, err = _run(c, f"cat {path}")
    if code != 0 or not out.strip():
        raise ValueError(f"empty {path}: {err}")
    return json.loads(out)


def main() -> None:
    load_env()
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

    RR, DS = "139.159.43.249", "139.159.43.208"
    op, ag = "http://127.0.0.1:8808", "http://127.0.0.1:9179"

    if not curl_save(c, f"{op}/api/bgp/neighbors", "/tmp/nb.json"):
        sys.exit(1)
    neighbors = cat_json(c, "/tmp/nb.json")

    print("=== RR 邻居行 ===")
    for n in neighbors:
        if n.get("vrf") == "gobgp-rr" and n.get("neighbor_ip") == RR:
            print(json.dumps(
                {k: n.get(k) for k in (
                    "session_state", "routes_received", "routes_sent",
                    "advertise_routes", "routes_cached", "source_ip",
                )},
                ensure_ascii=False,
                indent=2,
            ))

    print("\n=== 通告任务 ===")
    code, out, _ = _run(
        c,
        f'curl -sf -m 15 "{op}/api/bgp/neighbors/gobgp-rr/{RR}/advertise/status" -o /tmp/adv.json; echo exit=$?',
    )
    print(out.strip())
    if _run(c, "test -s /tmp/adv.json")[0] == 0:
        print(cat_json(c, "/tmp/adv.json"))

    ds_vrf = None
    print("\n=== 下游聚合源 ===")
    for n in neighbors:
        if str(n.get("role", "")).lower() == "downstream":
            sip = str(n.get("source_ip") or "")
            if sip == RR or n.get("neighbor_ip") == DS:
                print(
                    f"  vrf={n.get('vrf')} peer={n.get('neighbor_ip')} "
                    f"src={sip} rcvd={n.get('routes_received')} cached={n.get('routes_cached')}"
                )
            if n.get("neighbor_ip") == DS:
                ds_vrf = n.get("vrf")

    if ds_vrf:
        from urllib.parse import urlencode

        q = urlencode({"window": "downstream", "vrf": ds_vrf, "neighbor_ip": DS})
        curl_save(c, f"{ag}/api/rib/routes/count?{q}", "/tmp/cnt.json")
        print(f"\n=== 下游库 vrf={ds_vrf} count ===")
        print(cat_json(c, "/tmp/cnt.json"))
        curl_save(
            c,
            f"{ag}/api/rib/routes?{q}&page=1&page_size=25",
            "/tmp/rib.json",
        )
        rib = cat_json(c, "/tmp/rib.json")
        items = rib.get("items") or rib.get("routes") or []
        print(f"total={rib.get('total', len(items))} 样本（库内 nh，发 RR→207）:")
        for x in items[:25]:
            print(f"  {x.get('prefix',''):22s} nh={x.get('nexthop','')}")

    print("\n=== 最近聚合通告日志 ===")
    _, out, _ = _run(
        c,
        "journalctl -u bgp-agent --since '6 hours ago' --no-pager 2>/dev/null "
        "| grep -E 'gobgp-rr-139\\.159\\.43\\.249-advertise|rr-aggregate.*249' | tail -6",
    )
    print(out or "(无)")

    c.close()


if __name__ == "__main__":
    main()
