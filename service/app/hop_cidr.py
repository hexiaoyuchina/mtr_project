"""OP `match_cidr` 与 `te_rewrite_nfqueue` 展开语义一致。

写成「起始 IPv4 + /前缀」时，表示从该起始地址起连续 ``2^(32-pl)`` 个地址，
**不做 RFC 强行对齐**（与 ``ip_network(..., strict=False)`` 不同）。
"""
from __future__ import annotations

import ipaddress
from typing import Iterator


def ipv4_span_bounds(match_cidr: str) -> tuple[int, int] | None:
    """返回闭区间 [start_int, end_int]；非法则 None。"""
    raw = str(match_cidr).strip()
    if not raw:
        return None
    try:
        if "/" not in raw:
            x = int(ipaddress.IPv4Address(raw))
            return x, x
        base_raw, prefix_raw = raw.split("/", 1)
        base = ipaddress.IPv4Address(base_raw.strip())
        prefix_len = int(prefix_raw.strip())
        if prefix_len < 0 or prefix_len > 32:
            return None
        span = 1 << (32 - prefix_len)
        start = int(base)
        end = min(start + span - 1, int(ipaddress.IPv4Address("255.255.255.255")))
        return start, end
    except (ValueError, ipaddress.AddressValueError, TypeError):
        return None


def prefix_len_from_match(match_cidr: str) -> int:
    raw = str(match_cidr).strip()
    if "/" not in raw:
        return 32
    try:
        return int(raw.split("/", 1)[1].strip())
    except (ValueError, IndexError):
        return 32


def iter_ipv4_addresses(match_cidr: str, *, max_addresses: int) -> Iterator[ipaddress.IPv4Address]:
    bounds = ipv4_span_bounds(match_cidr)
    if not bounds:
        return
    start, end = bounds
    n = end - start + 1
    if n > max_addresses:
        raise ValueError(f"span has {n} addresses (max {max_addresses})")
    for i in range(start, end + 1):
        yield ipaddress.IPv4Address(i)
