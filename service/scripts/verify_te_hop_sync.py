#!/usr/bin/env python3
"""冒烟：hop 规则增删改后 /tmp/mtr_te_map.env 与 te_rewrite 日志一致（本机或 109 API）。"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("MTR_OP_API", "http://127.0.0.1:8808").rstrip("/")
MAP = Path(os.environ.get("MTR_TE_REWRITE_MAP_FILE", "/tmp/mtr_te_map.env"))
LOG = Path(os.environ.get("MTR_TE_REWRITE_LOG", "/tmp/te_rewrite_nfqueue.log"))
TEST_CIDR = os.environ.get("MTR_VERIFY_HOP_CIDR", "198.51.100.99/32")
TEST_FORGE_A = os.environ.get("MTR_VERIFY_HOP_FORGE_A", "198.51.100.1")
TEST_FORGE_B = os.environ.get("MTR_VERIFY_HOP_FORGE_B", "198.51.100.2")


def _req(method: str, path: str, body: dict | None = None) -> object:
    url = f"{API}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


def _map_text() -> str:
    if not MAP.is_file():
        return ""
    return MAP.read_text(encoding="ascii", errors="replace")


def _log_tail(n: int = 4000) -> str:
    if not LOG.is_file():
        return ""
    return LOG.read_text(encoding="utf-8", errors="replace")[-n:]


def main() -> int:
    g = _req("GET", "/api/global")
    if not g.get("hijack_enabled"):
        print("SKIP: hijack_enabled=false（先开总开关再测）", file=sys.stderr)
        return 2

    host = TEST_CIDR.split("/", 1)[0]
    rid = None
    try:
        row = _req(
            "POST",
            "/api/hop-rules",
            {
                "match_cidr": TEST_CIDR,
                "forged_src": TEST_FORGE_A,
                "priority": 0,
                "enabled": True,
                "note": "verify_te_hop_sync",
            },
        )
        rid = int(row["id"])
        m = _map_text()
        if f"{host}={TEST_FORGE_A}" not in m:
            print(f"FAIL add: map missing {host}={TEST_FORGE_A}\n{m}", file=sys.stderr)
            return 1

        _req(
            "PATCH",
            f"/api/hop-rules/{rid}",
            {"forged_src": TEST_FORGE_B},
        )
        m = _map_text()
        if f"{host}={TEST_FORGE_B}" not in m:
            print(f"FAIL patch: map missing {host}={TEST_FORGE_B}\n{m}", file=sys.stderr)
            return 1
        tail = _log_tail()
        if TEST_FORGE_B not in tail.split("reload rules=")[-1]:
            print(
                "WARN: te_rewrite log last reload may not show new forged "
                f"(check {LOG}); map file OK",
                file=sys.stderr,
            )

        _req("DELETE", f"/api/hop-rules/{rid}")
        rid = None
        m = _map_text()
        if f"{host}=" in m:
            print(f"FAIL delete: map still has {host}\n{m}", file=sys.stderr)
            return 1

        print("verify_te_hop_sync_ok")
        return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        return 1
    finally:
        if rid is not None:
            try:
                _req("DELETE", f"/api/hop-rules/{rid}")
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
