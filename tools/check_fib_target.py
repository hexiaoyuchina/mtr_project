#!/usr/bin/env python3
"""对照 BGP_FIB_TARGET 探测现网 OP + Agent 运行态。"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_env() -> None:
    p = Path(__file__).resolve().parent.parent / "109" / "env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.partition("=")[0], line.partition("=")[2]
            os.environ.setdefault(k.strip(), v.strip())


def get(url: str, timeout: int = 30) -> tuple[int | None, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body[:500]}
    except Exception as e:
        return None, {"error": str(e)}


def post(url: str, body: dict | None = None, timeout: int = 60) -> tuple[int | None, dict]:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:500]}
    except Exception as e:
        return None, {"error": str(e)}


def main() -> int:
    load_env()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    op = f"http://{host}:8808"
    ag = f"http://{host}:9179"
    out: dict = {"host": host}

    for name, path in [
        ("agent_health", "/health"),
        ("agent_status", "/api/status"),
        ("storage_stats", "/api/storage/stats"),
    ]:
        code, data = get(ag + path)
        out[name] = {"http": code, "data": data}

    for w in ("upstream", "downstream"):
        code, data = get(f"{ag}/api/fib/routes/count?window={w}")
        out[f"fib_count_{w}"] = {"http": code, **data}

    for w in ("upstream", "downstream"):
        code, data = get(f"{ag}/api/fib/routes?window={w}&page=1&page_size=2")
        out[f"fib_page_{w}"] = {
            "http": code,
            "total": data.get("total"),
            "sample": data.get("routes"),
        }

    code, data = get(f"{op}/api/bgp/neighbors")
    neighbors = data if isinstance(data, list) else []
    out["op_neighbors_http"] = code
    out["neighbors"] = []
    for n in neighbors:
        out["neighbors"].append(
            {
                "vrf": n.get("vrf"),
                "ip": n.get("neighbor_ip"),
                "role": n.get("role"),
                "enabled": n.get("enabled"),
                "state": n.get("session_state"),
                "source_ip": n.get("source_ip"),
                "store_received_routes": n.get("store_received_routes"),
                "advertise_routes": n.get("advertise_routes"),
                "routes_cached": n.get("routes_cached"),
                "routes_received": n.get("routes_received"),
            }
        )

    code, data = get(f"{op}/api/bgp/learned-routes/filter-options")
    out["learned_filter"] = {
        "http": code,
        "summary": data.get("summary"),
        "peers": len(data.get("peer_pairs") or []),
    }

    code, data = get(f"{op}/api/bgp/learned-routes?page=1&page_size=3")
    out["learned_routes"] = {
        "http": code,
        "total": data.get("total"),
        "summary": data.get("summary"),
        "sample": data.get("routes"),
    }

    code, data = get(f"{ag}/api/pipeline/consistency")
    out["pipeline_consistency"] = {"http": code, **data}

    rib_samples = []
    for n in out["neighbors"]:
        if n.get("role") == "downstream" and n.get("source_ip"):
            v, ip, sip = n["vrf"], n["ip"], n["source_ip"]
            code, data = get(
                f"{ag}/api/rib/routes/count?window=downstream&vrf={v}&neighbor_ip={ip}&source_ip={sip}"
            )
            rib_samples.append({"peer": f"{v}/{ip}@{sip}", "http": code, "count": data.get("count")})
        elif n.get("role") == "rr":
            v, ip = n.get("vrf") or "gobgp-rr", n["ip"]
            code, data = get(
                f"{ag}/api/rib/routes/count?window=upstream&vrf={v}&neighbor_ip={ip}"
            )
            rib_samples.append({"peer": f"{v}/{ip}", "http": code, "count": data.get("count")})
    out["rib_peer_samples"] = rib_samples

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
