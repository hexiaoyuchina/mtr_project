#!/usr/bin/env python3
"""
Linux 200：仅改写经转发的 ICMP Time Exceeded 外层 IPv4 源地址（不合成 Echo）。
配合 iptables：-t mangle -A FORWARD -p icmp --icmp-type time-exceeded -o ens192 -j NFQUEUE --queue-num 1

规则来源：逗号分隔环境变量 MTR_TE_REWRITE_MAP="旧IP=新IP,旧2=新2"
未设置或为空：不写任何替换，仅 NFQUEUE 直通（避免队列无监听丢包）。
"""
from __future__ import annotations

import os
import socket
import sys


def _parse_map() -> dict[str, str]:
    raw = (os.environ.get("MTR_TE_REWRITE_MAP") or "").strip()
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        a, b = part.split("=", 1)
        out[a.strip()] = b.strip()
    return out


def main() -> None:
    qn = int(os.environ.get("MTR_TE_QUEUE_NUM", "1"))
    rules = _parse_map()
    if rules:
        print(f"te_rewrite_nfqueue: queue={qn} rules={rules}", flush=True)
    else:
        print(
            f"te_rewrite_nfqueue: queue={qn} rules={{}} (pass-through, configure OP hop /32 or MTR_TE_REWRITE_MAP)",
            flush=True,
        )

    try:
        from netfilterqueue import NetfilterQueue  # type: ignore
    except ImportError as e:
        print("pip install NetfilterQueue", file=sys.stderr)
        raise SystemExit(1) from e

    try:
        from scapy.layers.inet import ICMP, IP  # type: ignore
    except ImportError as e:
        print("need scapy", file=sys.stderr)
        raise SystemExit(1) from e

    def cb(pkt) -> None:
        raw = pkt.get_payload()
        try:
            ip = IP(raw)
        except Exception:
            pkt.accept()
            return
        if ip.proto != socket.IPPROTO_ICMP:
            pkt.accept()
            return
        icmp = ip.payload
        if not isinstance(icmp, ICMP) or icmp.type != 11:
            pkt.accept()
            return
        old = ip.src
        new = rules.get(old)
        if not new:
            pkt.accept()
            return
        ip.src = new
        del ip.chksum
        del icmp.chksum
        pkt.set_payload(bytes(ip))
        pkt.accept()

    nfq = NetfilterQueue()
    nfq.bind(qn, cb)
    try:
        nfq.run()
    except KeyboardInterrupt:
        print("exit", flush=True)


if __name__ == "__main__":
    main()
