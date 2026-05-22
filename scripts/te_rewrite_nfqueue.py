#!/usr/bin/env python3
"""
改写经转发的 ICMP Time Exceeded 外层 IPv4 源地址（不依赖 scapy，避免 109 上 import 卡死）。
"""
from __future__ import annotations

import ast
import os
import signal
import socket
import struct
import sys
import threading
from pathlib import Path

_MAP_FILE = (os.environ.get("MTR_TE_REWRITE_MAP_FILE") or "/tmp/mtr_te_map.env").strip()
_rules_lock = threading.Lock()
_rules: dict[str, str] = {}


def _parse_map_line(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if "=" not in part:
            continue
        a, b = part.split("=", 1)
        out[a.strip()] = b.strip()
    return out


def _load_map_from_file(path: str) -> dict[str, str]:
    p = Path(path)
    if not p.is_file():
        return {}
    for line in p.read_text(encoding="ascii", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("export MTR_TE_REWRITE_MAP="):
            continue
        try:
            raw = ast.literal_eval(line.split("=", 1)[1].strip())
        except (SyntaxError, ValueError):
            continue
        if isinstance(raw, str):
            return _parse_map_line(raw)
    return {}


def _initial_rules() -> dict[str, str]:
    """启动：map 文件优先（与 OP 写库后落盘一致），env 仅作后备。"""
    from_file = _load_map_from_file(_MAP_FILE)
    if from_file:
        return from_file
    env_raw = (os.environ.get("MTR_TE_REWRITE_MAP") or "").strip()
    if env_raw:
        return _parse_map_line(env_raw)
    return {}


def _set_rules(new_rules: dict[str, str]) -> None:
    global _rules
    with _rules_lock:
        _rules = dict(new_rules)


def _get_rules() -> dict[str, str]:
    with _rules_lock:
        return dict(_rules)


def _reload_rules() -> None:
    """SIGHUP：仅以 map 文件为准（进程 env 里 MTR_TE_REWRITE_MAP 为启动时快照，会盖住新规则）。"""
    merged = _load_map_from_file(_MAP_FILE)
    if not merged:
        env_raw = (os.environ.get("MTR_TE_REWRITE_MAP") or "").strip()
        if env_raw:
            merged = _parse_map_line(env_raw)
    _set_rules(merged)
    print(f"te_rewrite_nfqueue: reload rules={merged}", flush=True)


def _inet4_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    n = len(data) // 2
    s = sum(struct.unpack(f"!{n}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return (~s) & 0xFFFF


def _rewrite_te_outer_src(raw: bytes, new_src: str) -> bytes | None:
    """仅处理 IPv4 + ICMP type 11；改外层源 IP 并重算 IP/ICMP 校验和。"""
    if len(raw) < 20:
        return None
    ver_ihl = raw[0]
    if (ver_ihl >> 4) != 4:
        return None
    ihl = (ver_ihl & 0x0F) * 4
    if ihl < 20 or len(raw) < ihl + 8:
        return None
    if raw[9] != socket.IPPROTO_ICMP:
        return None
    if raw[ihl] != 11:  # time exceeded
        return None
    try:
        new_addr = socket.inet_aton(new_src)
    except OSError:
        return None
    buf = bytearray(raw)
    buf[12:16] = new_addr
    buf[10:12] = b"\x00\x00"
    ip_csum = _inet4_checksum(bytes(buf[:ihl]))
    struct.pack_into("!H", buf, 10, ip_csum)
    buf[ihl + 2 : ihl + 4] = b"\x00\x00"
    icmp_csum = _inet4_checksum(bytes(buf[ihl:]))
    struct.pack_into("!H", buf, ihl + 2, icmp_csum)
    return bytes(buf)


def main() -> None:
    qn = int(os.environ.get("MTR_TE_QUEUE_NUM", "1"))
    _set_rules(_initial_rules())
    # 启动后改规则靠写 map 文件 + SIGHUP；勿让陈旧 env 在 reload 时覆盖文件。
    os.environ.pop("MTR_TE_REWRITE_MAP", None)
    rules = _get_rules()
    if rules:
        print(f"te_rewrite_nfqueue: queue={qn} rules={rules} map_file={_MAP_FILE}", flush=True)
    else:
        print(
            f"te_rewrite_nfqueue: queue={qn} rules={{}} (pass-through)",
            flush=True,
        )

    def on_hup(_signum: int, _frame: object) -> None:
        _reload_rules()

    signal.signal(signal.SIGHUP, on_hup)

    try:
        from netfilterqueue import NetfilterQueue  # type: ignore
    except ImportError as e:
        print("pip install NetfilterQueue", file=sys.stderr)
        raise SystemExit(1) from e

    def cb(pkt) -> None:
        raw = bytes(pkt.get_payload())
        if len(raw) < 20:
            pkt.accept()
            return
        ihl = (raw[0] & 0x0F) * 4
        if len(raw) < ihl + 1 or raw[9] != socket.IPPROTO_ICMP or raw[ihl] != 11:
            pkt.accept()
            return
        old_src = socket.inet_ntoa(raw[12:16])
        new_src = _get_rules().get(old_src)
        if not new_src:
            pkt.accept()
            return
        out = _rewrite_te_outer_src(raw, new_src)
        if out is None:
            pkt.accept()
            return
        pkt.set_payload(out)
        pkt.accept()

    print(f"te_rewrite_nfqueue: binding queue={qn} ...", flush=True)
    nfq = NetfilterQueue()
    nfq.bind(qn, cb)
    print(f"te_rewrite_nfqueue: bound queue={qn}", flush=True)
    try:
        nfq.run()
    except KeyboardInterrupt:
        print("exit", flush=True)


if __name__ == "__main__":
    main()
