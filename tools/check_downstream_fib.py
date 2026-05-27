#!/usr/bin/env python3
"""诊断 downstream FIB 为何未重算。"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import paramiko
except ImportError:
    paramiko = None


def load_env() -> str:
    p = Path(__file__).resolve().parent.parent / "109" / "env"
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    return os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()


def get(url: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def post(url: str, timeout: int = 120) -> tuple[int | None, dict | str]:
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:500]
    except Exception as e:
        return None, str(e)


def main() -> int:
    host = load_env()
    ag = f"http://{host}:9179"
    out: dict = {}

    out["policy_208"] = get(
        f"{ag}/api/rib/policy?vrf=vbgp13915943249&neighbor_ip=139.159.43.208"
    )
    out["rib_208"] = get(
        f"{ag}/api/rib/routes/count?window=downstream"
        f"&vrf=vbgp13915943249&neighbor_ip=139.159.43.208&source_ip=139.159.43.249"
    )
    out["fib_before"] = get(f"{ag}/api/fib/routes/count?window=downstream")
    out["freeze"] = get(f"{ag}/api/peers/freeze-status")

    code, rec = post(f"{ag}/api/fib/recompute?window=downstream", timeout=180)
    out["recompute"] = {"http": code, "body": rec}
    out["fib_after_recompute"] = get(f"{ag}/api/fib/routes/count?window=downstream")
    out["fib_routes"] = get(f"{ag}/api/fib/routes?window=downstream&page=1&page_size=5")

    if paramiko and os.environ.get("MTR_OP_SSH_PASSWORD"):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            host,
            username="root",
            password=os.environ["MTR_OP_SSH_PASSWORD"],
            timeout=30,
            allow_agent=False,
            look_for_keys=False,
        )
        _, stdout, _ = c.exec_command(
            "journalctl -u bgp-agent -n 150 --no-pager 2>/dev/null | "
            "grep -iE 'fib|policy|208|downstream|recompute|peer policy' | tail -30",
            timeout=30,
        )
        out["journal"] = stdout.read().decode(errors="replace")
        c.close()

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
