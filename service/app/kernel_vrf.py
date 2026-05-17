"""Linux 内核 VRF 设备（``ip link type vrf``），与 BGP 控制面无关。"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Dict, List, Optional

_KERNEL_VRF_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,14}$")


class KernelVrfError(RuntimeError):
    pass


def kernel_vrf_devices() -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        p = subprocess.run(
            ["ip", "-j", "link", "show", "type", "vrf"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if p.returncode != 0 or not (p.stdout or "").strip():
            return out
        data = json.loads(p.stdout)
        if not isinstance(data, list):
            return out
        for row in data:
            if not isinstance(row, dict):
                continue
            ifname = (row.get("ifname") or "").strip()
            if not ifname:
                continue
            info = row.get("linkinfo") if isinstance(row.get("linkinfo"), dict) else {}
            kind = (info.get("info_kind") or row.get("link_type") or "").strip().lower()
            if kind != "vrf" and row.get("link_type") != "vrf":
                continue
            info_data = info.get("info_data") if isinstance(info.get("info_data"), dict) else {}
            try:
                tid = int(info_data.get("table") or 0)
            except (TypeError, ValueError):
                tid = 0
            if tid > 0:
                out[ifname] = tid
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError, ValueError):
        return out
    return out


def list_kernel_vrf_names() -> List[str]:
    return sorted(kernel_vrf_devices().keys())


def validate_kernel_vrf_ifname(vrf: str) -> str:
    vrf_n = (vrf or "").strip()
    if vrf_n == "default":
        raise KernelVrfError("kernel_vrf_reserved_default")
    if not _KERNEL_VRF_NAME_RE.match(vrf_n):
        raise KernelVrfError(
            "kernel_vrf_invalid_name: VRF 须为合法接口名（1–15 字符，字母数字与 . _ -）"
        )
    return vrf_n


def allocate_rt_table_for_kernel_vrf() -> int:
    used = set(kernel_vrf_devices().values())
    lo = int(os.environ.get("MTR_BGP_AUTO_VRF_TABLE_MIN") or "30200")
    hi = int(os.environ.get("MTR_BGP_AUTO_VRF_TABLE_MAX") or "64999")
    for t in range(lo, hi + 1):
        if t not in used:
            return t
    raise KernelVrfError("no_free_rt_table_for_kernel_vrf")


def ensure_kernel_vrf(vrf: str, rt_table: Optional[int] = None) -> Dict[str, Any]:
    vrf_n = validate_kernel_vrf_ifname(vrf)
    existing = kernel_vrf_devices()
    if vrf_n in existing:
        return {"vrf": vrf_n, "rt_table": int(existing[vrf_n]), "created": False}
    tid = int(rt_table) if rt_table is not None else allocate_rt_table_for_kernel_vrf()
    used = set(existing.values())
    if tid in used:
        tid = allocate_rt_table_for_kernel_vrf()
    p = subprocess.run(
        ["ip", "link", "add", vrf_n, "type", "vrf", "table", str(tid)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    err = ((p.stderr or "") + (p.stdout or "")).strip()
    if p.returncode != 0:
        if "File exists" in err or "exists" in err.lower():
            return {"vrf": vrf_n, "rt_table": tid, "created": False, "note": "race_or_exists"}
        raise KernelVrfError(f"kernel_vrf_create_failed: {err[:400]}")
    subprocess.run(["ip", "link", "set", vrf_n, "up"], capture_output=True, text=True, timeout=8, check=False)
    return {"vrf": vrf_n, "rt_table": tid, "created": True}


def ebgp_multihop_satellite_default() -> Optional[int]:
    raw = (os.environ.get("MTR_SATELLITE_BGP_EBGP_MULTIHOP") or "5").strip().lower()
    if raw in {"", "0", "off", "no", "false"}:
        return None
    try:
        n = int(raw)
        return n if n > 0 else None
    except ValueError:
        return 5
