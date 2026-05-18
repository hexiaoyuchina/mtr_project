"""卫星 VRF 命名：``{MTR_SATELLITE_VRF_PREFIX}{IPv4 去点}``，如 ``10.133.152.233`` → ``vbgp10133152233``。"""
from __future__ import annotations

import os
from typing import Optional


def ip_without_dots(ip: str) -> str:
    return (ip or "").strip().replace(".", "")


def vrf_prefix() -> str:
    p = (os.environ.get("MTR_SATELLITE_VRF_PREFIX") or "vbgp").strip()
    return p if p else "vbgp"


def satellite_vrf_name(ip: str, prefix: Optional[str] = None) -> str:
    """由冒充/卫星 IPv4 生成 VRF 名（不用末字节，避免与 ``vbgp233`` 混淆）。"""
    nodots = ip_without_dots(ip)
    if not nodots:
        return ""
    p = (prefix or vrf_prefix()).strip() or "vbgp"
    return f"{p}{nodots}"
