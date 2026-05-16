#!/usr/bin/env python3
"""
[已弃用 — 实验室默认改用 nft postrouting SNAT 改写真实 ICMP TE，见 service/app/nft_sync.py]

Linux 200：对途经本机的 ICMP Echo-request（mtr 探测）经 NFQUEUE 拦截；命中 hop_replace_rules 的跳
将 ICMP Time Exceeded 的外层源地址替换为 forged_src，未命中规则的跳仍使用探测得到的真实 hop IP。

合成 TE 的跳数必须与实际探测路径长度一致：若 hop 表过短，TTL 较大时会提前进入 Echo Reply，
mtr 只剩 1～2 跳。探测默认与机器 A 同路径（SSH 到 201 时使用 -I/-a/-m），必要时用 traceroute 补全长路径。

伪造跳数 = 探测得到的 hop 列表长度。若本机 vrf 到目的路径与业务客户端不一致，可设置 **MTR_PROBE_SSH_HOST**
在非本机、与业务一致的 vrf 内执行 mtr；失败时再回退本机探测（`MTR_PROBE_LOCAL_VRF_EXEC`）。

可选 --prefix-hop-ips（逗号分隔）拼在探测路径前，便于与客户端 TTL 对齐。

规则：priority + 最长前缀匹配 forged_src；未命中则 TE 源用探测 hop IP。

环境：MTR_OP_DB；MTR_PROBE_SSH_HOST；MTR_PROBE_VRF_EXEC；MTR_PROBE_LOCAL_VRF_EXEC；
MTR_PROBE_MTR_COUNT；MTR_HOP_PREFIX_IPS。

**同步实时探测**（`--sync-probe` / `MTR_PROBE_SYNC=1`）：在 NFQUEUE 回调里**现跑** mtr/traceroute，不再依赖后台
`probe_loop` 周期缓存；可用 `MTR_PROBE_SYNC_CACHE_SEC` 控制同一目的内存复用秒数（默认 5s；设 0 则每包现测）。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import random
import re
import shlex
import sqlite3
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

IPv4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
)

# mtr/traceroute 无应答占位；TE 外层源使用 RFC 5737 TEST-NET-1（192.0.2.0/24）
UNKNOWN_HOP_MARK = "???"


def _unknown_hop_te_src(slot: int) -> str:
    """??? 跳无法匹配规则时的合成 ICMP TE 源地址（需为合法 IPv4 供 scapy 发包）。"""
    n = (int(slot) - 1) % 254 + 1
    return f"192.0.2.{n}"


def _finalize_probe_path(path: list[str], dst_ip: str) -> list[str]:
    """探测链末尾强制包含目的 IPv4（与 NFQUEUE 见到的 Echo 目的对齐），便于缓存对照；保留 ??? 占位。"""
    p = list(path)
    try:
        dip = ipaddress.IPv4Address(str(dst_ip).strip())
    except ValueError:
        return p
    ds = str(dip)
    if not p or p[-1] != ds:
        p.append(ds)
    return p


def _default_probe_mtr_count() -> int:
    raw = (os.environ.get("MTR_PROBE_MTR_COUNT") or "15").strip()
    try:
        return max(1, min(99, int(raw)))
    except ValueError:
        return 15


def _default_probe_mtr_extra() -> str:
    """附加到 `mtr -r -n` 与 `-c` 之间，例如：-4 -m 32 -I ens192 -a 10.133.152.204"""
    return (os.environ.get("MTR_PROBE_MTR_EXTRA") or "").strip()


def _default_probe_min_hops() -> int:
    raw = (os.environ.get("MTR_PROBE_MIN_HOPS") or "8").strip()
    try:
        return max(2, min(64, int(raw)))
    except ValueError:
        return 8


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _default_max_synthetic_hops() -> int:
    """NFQUEUE 只对 TTL 映射的前 N 跳合成 TE；探测链过长时否则会长期 hop_index < len 导致 Echo 无法转发（ping 全丢）。"""
    raw = (os.environ.get("MTR_NFQ_MAX_SYNTHETIC_HOPS") or "32").strip()
    try:
        return max(8, min(96, int(raw)))
    except ValueError:
        return 32


def _default_sync_probe_cache_sec() -> float:
    raw = (os.environ.get("MTR_PROBE_SYNC_CACHE_SEC") or "5").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


def _default_probe_merge_traceroute() -> bool:
    """默认再跑一遍 traceroute，与 mtr 解析结果取较长路径（mtr 报表里 ??? 无 IP 会被跳过，链易偏短）。"""
    raw = (os.environ.get("MTR_PROBE_MERGE_TRACEROUTE") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _parse_bind_iface_from_mtr_extra(extra: str) -> tuple[str, str]:
    """从探测附加参数里取出 -a、-I，供 traceroute -s/-i 复用。"""
    src, iface = "", ""
    try:
        parts = shlex.split(extra)
    except ValueError:
        return "", ""
    i = 0
    while i < len(parts):
        if parts[i] == "-a" and i + 1 < len(parts):
            src = parts[i + 1]
            i += 2
        elif parts[i] in ("-I", "--interface") and i + 1 < len(parts):
            iface = parts[i + 1]
            i += 2
        else:
            i += 1
    return src, iface


def _long_fallback_te_chain(slots: int) -> list[HopEntry]:
    """探测尚未入库时避免仅 1 条 TE 导致路径塌缩；各槽位不同文档地址便于区分。"""
    n = max(8, min(96, int(slots)))
    out: list[HopEntry] = []
    for i in range(n):
        last_oct = (i % 250) + 1
        sip = f"203.0.113.{last_oct}"
        out.append(
            HopEntry(
                icmp_src=sip,
                delay_min_ms=0,
                delay_max_ms=0,
                outbound_ttl=64,
                probed_ip=f"fallback-slot-{i}",
                rule_id=None,
            )
        )
    return out


def _parse_prefix_ips(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


@dataclass
class HopEntry:
    icmp_src: str
    delay_min_ms: int
    delay_max_ms: int
    outbound_ttl: int
    probed_ip: str
    rule_id: Optional[int] = None


def _iface_for_client(src: str, m201: str, m202: str, i201: str, i202: str) -> str:
    if src == m201:
        return i201
    if src == m202:
        return i202
    return i201


def _run_cmd(cmd: list[str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + "\n" + p.stderr
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except OSError as e:
        return -1, str(e)


def _parse_mtr_report(text: str) -> list[str]:
    """解析 mtr -r -n 报表：有 IPv4 则收录；无地址但为未知跳（???）时收录占位符，保持跳序。"""
    hops: list[str] = []
    for line in text.splitlines():
        if "|--" not in line and "|?" not in line:
            continue
        found = IPv4_RE.findall(line)
        if found:
            ip = found[0]
            if ip == "0.0.0.0":
                continue
            if hops and hops[-1] == ip:
                continue
            hops.append(ip)
            continue
        # 常见：`  8.|-- ???`（不用主机名行兜底，避免误插占位）
        if "???" in line:
            if hops and hops[-1] == UNKNOWN_HOP_MARK:
                continue
            hops.append(UNKNOWN_HOP_MARK)
    return hops


def _parse_traceroute(text: str) -> list[str]:
    hops: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("traceroute"):
            continue
        found = IPv4_RE.findall(line)
        if found:
            ip = found[0]
            if hops and hops[-1] == ip:
                continue
            hops.append(ip)
            continue
        # ` 5 * * *` 等超时行：占位以保持跳序
        if re.match(r"^\d+", line) and "*" in line:
            if hops and hops[-1] == UNKNOWN_HOP_MARK:
                continue
            hops.append(UNKNOWN_HOP_MARK)
    return hops


def _pick_longer_probe_path(mtr_hops: list[str], tt_hops: list[str]) -> list[str]:
    """相同条件下优先保留 mtr 结果；traceroute 更长时用 traceroute（常见：mtr 中间多为 ???）。"""
    if len(tt_hops) > len(mtr_hops):
        return tt_hops
    return mtr_hops if mtr_hops else tt_hops


def _ssh_probe_path(
    host: str,
    user: str,
    password: str,
    dst: str,
    timeout: float,
    prefer_mtr: bool,
    vrf_exec: str,
    probe_mtr_count: int,
    mtr_extra: str,
) -> list[str]:
    """在客户端主机上执行 mtr/traceroute；mtr_extra 插在 `-r -n` 与 `-c` 之间（如 -4 -m 32 -I ens -a）。"""
    mtr_c = max(1, min(int(probe_mtr_count), 99))
    extra_tokens: list[str] = []
    if (mtr_extra or "").strip():
        try:
            extra_tokens = shlex.split(mtr_extra.strip())
        except ValueError:
            extra_tokens = []
    if prefer_mtr:
        mtr_argv = ["mtr", "-r", "-n", *extra_tokens, "-c", str(mtr_c), dst]
        inner_cmd = " ".join(shlex.quote(x) for x in mtr_argv)
    else:
        tt = ["traceroute", "-n", "-q", "1", "-w", "2", "-m", "32"]
        bs, bi = _parse_bind_iface_from_mtr_extra(mtr_extra)
        if bs:
            tt.extend(["-s", bs])
        if bi:
            tt.extend(["-i", bi])
        tt.append(dst)
        inner_cmd = " ".join(shlex.quote(x) for x in tt)
    inner = f"{vrf_exec.strip()} {inner_cmd}" if (vrf_exec or "").strip() else inner_cmd
    try:
        import paramiko  # type: ignore
    except ImportError:
        return []
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            host,
            username=user,
            password=password,
            timeout=min(20.0, timeout),
            allow_agent=False,
            look_for_keys=False,
        )
        _, stdout, stderr = c.exec_command(inner, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace") + stderr.read().decode(
            "utf-8", errors="replace"
        )
        c.close()
    except Exception:
        return []
    if prefer_mtr:
        hops = _parse_mtr_report(out)
        if hops:
            return hops
    return _parse_traceroute(out)


def _ssh_traceroute_path(
    host: str,
    user: str,
    password: str,
    dst: str,
    timeout: float,
    vrf_exec: str,
    bind_src: str,
    bind_if: str,
) -> list[str]:
    """SSH 上 traceroute，尽量与 mtr 同源地址/出接口，补全长路径跳数。"""
    argv = ["traceroute", "-n", "-q", "1", "-w", "2", "-m", "32"]
    if bind_src:
        argv.extend(["-s", bind_src])
    if bind_if:
        argv.extend(["-i", bind_if])
    argv.append(dst)
    inner_cmd = " ".join(shlex.quote(x) for x in argv)
    inner = f"{vrf_exec.strip()} {inner_cmd}" if (vrf_exec or "").strip() else inner_cmd
    try:
        import paramiko  # type: ignore
    except ImportError:
        return []
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            host,
            username=user,
            password=password,
            timeout=min(20.0, timeout),
            allow_agent=False,
            look_for_keys=False,
        )
        _, stdout, stderr = c.exec_command(inner, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace") + stderr.read().decode(
            "utf-8", errors="replace"
        )
        c.close()
    except Exception:
        return []
    return _parse_traceroute(out)


def _probe_path_local(
    dst: str,
    timeout: float,
    prefer_mtr: bool,
    probe_mtr_count: int,
    local_vrf_exec: str,
    mtr_extra: str,
    probe_merge_traceroute: bool,
    min_probe_hops: int,
    verbose: bool,
) -> list[str]:
    """在 Linux 200 本机执行 mtr/traceroute；local_vrf_exec 非空时在其上下文中执行（如 ip vrf exec vrf2103）。"""
    mtr_c = str(max(1, min(int(probe_mtr_count), 99)))
    vrf = (local_vrf_exec or "").strip()
    extra_tokens: list[str] = []
    if (mtr_extra or "").strip():
        try:
            extra_tokens = shlex.split(mtr_extra.strip())
        except ValueError:
            extra_tokens = []

    def _wrap(argv: list[str]) -> list[str]:
        if not vrf:
            return argv
        inner = " ".join(shlex.quote(x) for x in argv)
        return ["bash", "-lc", vrf + " " + inner]

    mtr_hops: list[str] = []
    if prefer_mtr:
        _, out = _run_cmd(
            _wrap(["mtr", "-r", "-n", *extra_tokens, "-c", mtr_c, dst]),
            timeout=timeout,
        )
        if out:
            mtr_hops = _parse_mtr_report(out)

    need_tt = (
        probe_merge_traceroute
        or (prefer_mtr and len(mtr_hops) < min_probe_hops)
        or not mtr_hops
    )
    tt_hops: list[str] = []
    if need_tt:
        _, out = _run_cmd(
            _wrap(["traceroute", "-n", "-q", "1", "-w", "2", "-m", "32", dst]),
            timeout=timeout,
        )
        if out:
            tt_hops = _parse_traceroute(out)

    picked = _pick_longer_probe_path(mtr_hops, tt_hops)
    if verbose and len(tt_hops) > len(mtr_hops):
        print(
            f"probe: local traceroute longer ({len(tt_hops)}>{len(mtr_hops)}) dst={dst}",
            flush=True,
        )
    return picked


def _probe_path_to_dst(
    dst: str,
    timeout: float,
    prefer_mtr: bool,
    probe_ssh_host: Optional[str],
    probe_ssh_user: str,
    probe_ssh_password: str,
    probe_vrf_exec: str,
    probe_mtr_count: int,
    probe_local_vrf_exec: str,
    mtr_extra: str,
    min_probe_hops: int,
    probe_merge_traceroute: bool,
    verbose: bool,
) -> list[str]:
    ve = (probe_vrf_exec or "").strip()
    if probe_ssh_host:
        hops = _ssh_probe_path(
            probe_ssh_host,
            probe_ssh_user,
            probe_ssh_password,
            dst,
            timeout,
            prefer_mtr,
            ve,
            probe_mtr_count,
            mtr_extra,
        )
        bind_src, bind_if = _parse_bind_iface_from_mtr_extra(mtr_extra)
        need_tt = (
            probe_merge_traceroute
            or (prefer_mtr and len(hops) < min_probe_hops)
            or not hops
        )
        if need_tt:
            alt = _ssh_traceroute_path(
                probe_ssh_host,
                probe_ssh_user,
                probe_ssh_password,
                dst,
                timeout,
                ve,
                bind_src,
                bind_if,
            )
            if len(alt) > len(hops):
                if verbose:
                    print(
                        f"probe: ssh traceroute longer ({len(alt)}>{len(hops)}) dst={dst}",
                        flush=True,
                    )
                hops = alt
        if hops:
            if verbose:
                print(
                    f"probe: ssh {probe_ssh_host} hops={len(hops)} dst={dst}",
                    flush=True,
                )
            return hops
        if verbose:
            print(
                f"probe: ssh {probe_ssh_host} empty/fail -> fallback local mtr dst={dst}",
                flush=True,
            )
    return _probe_path_local(
        dst,
        timeout,
        prefer_mtr,
        probe_mtr_count,
        probe_local_vrf_exec,
        mtr_extra,
        probe_merge_traceroute,
        min_probe_hops,
        verbose,
    )


def _match_cidr_span(hop_ip: str, cidr: str) -> tuple[bool, int]:
    """按 OP 填写的起始 IP + 前缀长度做范围匹配。

    `61.49.37.90/30` 在这里表示从 `.90` 开始连续 4 个地址，而不是标准
    CIDR 对齐后的 `.88/30`。返回 (是否命中, prefixlen)，用于同优先级下选更小范围。
    """
    try:
        ip = ipaddress.IPv4Address(hop_ip)
    except ValueError:
        return False, 0

    raw = str(cidr).strip()
    try:
        if "/" not in raw:
            return ip == ipaddress.IPv4Address(raw), 32

        base_raw, prefix_raw = raw.split("/", 1)
        base = ipaddress.IPv4Address(base_raw.strip())
        prefix_len = int(prefix_raw.strip())
        if prefix_len < 0 or prefix_len > 32:
            return False, 0

        span = 1 << (32 - prefix_len)
        start = int(base)
        end = min(start + span - 1, int(ipaddress.IPv4Address("255.255.255.255")))
        return start <= int(ip) <= end, prefix_len
    except (ValueError, ipaddress.AddressValueError):
        return False, 0


def _pick_rule(hop_ip: str, rules: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """在覆盖 hop_ip 的规则中选一条：priority 较大者优先；同 priority 时更小范围优先。"""
    cand: list[tuple[int, int, dict[str, Any]]] = []
    for r in rules:
        matched, prefix_len = _match_cidr_span(hop_ip, str(r["match_cidr"]).strip())
        if not matched:
            continue
        pri = int(r.get("priority", 0))
        cand.append((pri, prefix_len, r))
    if not cand:
        return None
    cand.sort(key=lambda x: (-x[0], -x[1]))
    return cand[0][2]


def build_hops_from_probe(
    path: list[str],
    dst_ip: str,
    rules: list[dict[str, Any]],
) -> tuple[list[HopEntry], str]:
    """对探测路径逐跳匹配规则。顺序须与客户端 Echo 的 TTL 一致（hop_index = ttl-1）。

    路径会先 **末尾补齐目的 IPv4**（与 Echo 目的一致），再去掉目的仅保留中间跳用于 TE。
    `???` 表示无应答跳：不参与规则匹配，TE 源使用 192.0.2.x。

    若本机只探测「200→目的」，可用命令行 --prefix-hop-ips 在路径前拼接若干跳。返回 (hops, note)。"""
    dip_s = ""
    try:
        dip_s = str(ipaddress.IPv4Address(str(dst_ip).strip()))
    except ValueError:
        pass

    if dip_s:
        p = _finalize_probe_path(list(path), dip_s)
    else:
        p = list(path)

    if not p:
        return [], "empty_probe"

    if dip_s and p[-1] == dip_s:
        body = p[:-1]
    else:
        body = list(p)
        if body and body[-1] == str(dst_ip).strip():
            body = body[:-1]

    out: list[HopEntry] = []
    unk_slot = 0
    for hop_ip in body:
        if hop_ip == UNKNOWN_HOP_MARK:
            unk_slot += 1
            src = _unknown_hop_te_src(unk_slot)
            out.append(
                HopEntry(
                    icmp_src=src,
                    delay_min_ms=0,
                    delay_max_ms=0,
                    outbound_ttl=64,
                    probed_ip=UNKNOWN_HOP_MARK,
                    rule_id=None,
                )
            )
            continue
        r = _pick_rule(hop_ip, rules)
        if r:
            out.append(
                HopEntry(
                    icmp_src=str(r["forged_src"]).strip(),
                    delay_min_ms=max(0, int(r["delay_min_ms"])),
                    delay_max_ms=max(0, int(r["delay_max_ms"])),
                    outbound_ttl=max(1, min(255, int(r["icmp_ip_ttl"]))),
                    probed_ip=hop_ip,
                    rule_id=int(r["id"]),
                )
            )
        else:
            out.append(
                HopEntry(
                    icmp_src=hop_ip,
                    delay_min_ms=0,
                    delay_max_ms=0,
                    outbound_ttl=64,
                    probed_ip=hop_ip,
                    rule_id=None,
                )
            )

    if not out:
        return [], "degenerate"

    return out, "ok"


class RuleCache:
    def __init__(self, db_path: Path, reload_sec: float) -> None:
        self._db_path = db_path
        self._reload_sec = max(1.0, reload_sec)
        self._lock = threading.Lock()
        self._rules: list[dict[str, Any]] = []
        self._last_load = 0.0

    def get(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            if now - self._last_load >= self._reload_sec or not self._rules:
                self._reload_unlocked()
                self._last_load = now
            return list(self._rules)

    def _reload_unlocked(self) -> None:
        if not self._db_path.is_file():
            self._rules = []
            return
        try:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, match_cidr, forged_src, delay_min_ms, delay_max_ms, icmp_ip_ttl, priority "
                "FROM hop_replace_rules WHERE enabled = 1 ORDER BY priority DESC, id ASC"
            ).fetchall()
            conn.close()
            self._rules = [dict(r) for r in rows]
        except OSError:
            self._rules = []


def hop_entries_to_json(hops: list[HopEntry]) -> list[dict[str, Any]]:
    return [
        {
            "icmp_src": h.icmp_src,
            "delay_min_ms": h.delay_min_ms,
            "delay_max_ms": h.delay_max_ms,
            "outbound_ttl": h.outbound_ttl,
            "probed_ip": h.probed_ip,
            "rule_id": h.rule_id,
        }
        for h in hops
    ]


@dataclass
class DstCache:
    hops: list[HopEntry]
    probed: list[str]
    updated_at: float = 0.0
    note: str = ""


class ActiveDstSet:
    """从 NFQUEUE 见到的 Echo 目的 IPv4 收集探测目标；超出上限时丢弃最早已记录地址。"""

    def __init__(self, max_items: int) -> None:
        self._max = max(16, max_items)
        self._lock = threading.Lock()
        self._order: deque[str] = deque()
        self._seen: set[str] = set()

    def add(self, dst: str) -> bool:
        with self._lock:
            if dst in self._seen:
                return False
            self._seen.add(dst)
            self._order.append(dst)
            while len(self._order) > self._max:
                old = self._order.popleft()
                self._seen.discard(old)
            return True

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._order)


class HopStore:
    def __init__(self, cache_file: str | None) -> None:
        self._lock = threading.Lock()
        self._m: dict[str, DstCache] = {}
        self._cache_file = cache_file

    def get_hops(self, dst: str) -> list[HopEntry] | None:
        with self._lock:
            e = self._m.get(dst)
            return list(e.hops) if e and e.hops else None

    def set_dst(self, dst: str, hops: list[HopEntry], probed: list[str], note: str) -> None:
        with self._lock:
            self._m[dst] = DstCache(hops=list(hops), probed=list(probed), updated_at=time.time(), note=note)
            self._write_unlocked()

    def _write_unlocked(self) -> None:
        if not self._cache_file:
            return
        try:
            os.makedirs(os.path.dirname(self._cache_file) or ".", exist_ok=True)
            tmp = self._cache_file + ".tmp"
            snap = {
                k: {
                    "hops": hop_entries_to_json(v.hops),
                    "probed": v.probed,
                    "updated_at": v.updated_at,
                    "note": v.note,
                }
                for k, v in self._m.items()
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._cache_file)
        except OSError:
            pass


def probe_loop(
    store: HopStore,
    rules_cache: RuleCache,
    active_dsts: ActiveDstSet,
    interval: float,
    probe_timeout: float,
    prefer_mtr: bool,
    probe_ssh_host: Optional[str],
    probe_ssh_user: str,
    probe_ssh_password: str,
    probe_vrf_exec: str,
    probe_mtr_count: int,
    probe_local_vrf_exec: str,
    probe_mtr_extra: str,
    probe_min_hops: int,
    probe_merge_traceroute: bool,
    prefix_hop_ips: list[str],
    rng: random.Random,
    verbose: bool,
    wake: threading.Event,
    stop: threading.Event,
) -> None:
    while not stop.is_set():
        wake.clear()
        dsts = active_dsts.snapshot()
        # 队列末尾是最新见到的目的；先探测新目的，缩短「换域名首跑无缓存」窗口。
        dsts = list(reversed(dsts))
        rules = rules_cache.get()
        if verbose and dsts:
            print(f"probe: active_dsts(newest-first)={dsts} rules={len(rules)}", flush=True)
        for dst in dsts:
            if stop.is_set():
                break
            path = _probe_path_to_dst(
                dst,
                timeout=probe_timeout,
                prefer_mtr=prefer_mtr,
                probe_ssh_host=probe_ssh_host,
                probe_ssh_user=probe_ssh_user,
                probe_ssh_password=probe_ssh_password,
                probe_vrf_exec=probe_vrf_exec,
                probe_mtr_count=probe_mtr_count,
                probe_local_vrf_exec=probe_local_vrf_exec,
                mtr_extra=probe_mtr_extra,
                min_probe_hops=probe_min_hops,
                probe_merge_traceroute=probe_merge_traceroute,
                verbose=verbose,
            )
            raw_path = list(prefix_hop_ips) + path if prefix_hop_ips else path
            try:
                dip_s = str(ipaddress.IPv4Address(str(dst).strip()))
                full_path = _finalize_probe_path(raw_path, dip_s)
            except ValueError:
                full_path = list(raw_path)
            hops, note = build_hops_from_probe(full_path, dst, rules)
            store.set_dst(dst, hops, full_path, note)
            if verbose:
                print(
                    f"probe: dst={dst} note={note} hops={len(hops)} "
                    f"probed_path_len={len(full_path)} prefix={len(prefix_hop_ips)}",
                    flush=True,
                )
        if wake.wait(timeout=interval):
            continue
        if stop.is_set():
            break


def _cache_miss_action(raw: str) -> str:
    action = (raw or "accept").strip().lower()
    if action in {"accept", "fallback"}:
        return action
    return "accept"


def _has_non_fallback_hops(hops: list[HopEntry] | None) -> bool:
    return bool(hops) and not all(str(h.probed_ip).startswith("fallback-slot-") for h in hops)


def _maybe_delay(entry: HopEntry, rng: random.Random) -> None:
    mn, mx = entry.delay_min_ms, entry.delay_max_ms
    if mx <= 0:
        return
    if mn > mx:
        mn, mx = mx, mn
    delay_s = rng.uniform(mn, mx) / 1000.0
    if delay_s > 0:
        time.sleep(delay_s)


_LOCAL_IPV4_TS: float = -1e9
_LOCAL_IPV4_SET: frozenset[str] | None = None


def local_ipv4_addresses(refresh_sec: float = 30.0) -> frozenset[str]:
    """本机非回环 IPv4（各接口含 vrf）。目的为本机的 ICMP Echo 应交还内核代答，勿走逐 hop TE/drop。"""
    global _LOCAL_IPV4_TS, _LOCAL_IPV4_SET
    now = time.monotonic()
    if _LOCAL_IPV4_SET is not None and now - _LOCAL_IPV4_TS < refresh_sec:
        return _LOCAL_IPV4_SET
    ips: set[str] = set()
    try:
        p = subprocess.run(
            ["ip", "-j", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if p.returncode == 0 and (p.stdout or "").strip():
            for iface in json.loads(p.stdout):
                for a in iface.get("addr_info", []):
                    if a.get("family") != "inet":
                        continue
                    loc = (a.get("local") or "").strip()
                    if loc and not loc.startswith("127."):
                        ips.add(loc)
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, TypeError, KeyError, ValueError):
        pass
    if not ips:
        try:
            p2 = subprocess.run(
                ["ip", "-4", "addr", "show"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for m in IPv4_RE.finditer(p2.stdout or ""):
                a = m.group(0)
                if not a.startswith("127."):
                    ips.add(a)
        except (OSError, subprocess.TimeoutExpired):
            pass
    out = frozenset(ips)
    _LOCAL_IPV4_TS = now
    _LOCAL_IPV4_SET = out
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="NFQUEUE MTR spoof — 200→dest per-hop rules")
    parser.add_argument("--queue-num", type=int, default=0)
    parser.add_argument("--target", default=None, help="强制 Echo Reply 源 IP（默认 ip.dst）")
    parser.add_argument("--host-201", default="10.133.152.204")
    parser.add_argument("--host-202", default="10.133.152.205")
    parser.add_argument("--iface-201", default="ens192")
    parser.add_argument("--iface-202", default="ens161")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--probe-interval", type=float, default=45.0)
    parser.add_argument("--probe-timeout", type=float, default=60.0)
    parser.add_argument(
        "--probe-mtr-count",
        type=int,
        default=_default_probe_mtr_count(),
        metavar="N",
        help="SSH/本机 mtr 探测时每目的采样轮数 -c N（默认 env MTR_PROBE_MTR_COUNT 或 15）",
    )
    parser.add_argument(
        "--cache-file",
        default="/tmp/mtr_spoof_chain.json",
        help="调试 JSON；空字符串禁用",
    )
    parser.add_argument("--no-mtr", action="store_true", help="仅用 traceroute")
    parser.add_argument(
        "--probe-local-only",
        action="store_true",
        help="强制仅用本机探测（忽略 --probe-ssh-host）",
    )
    parser.add_argument(
        "--probe-ssh-host",
        default=os.environ.get("MTR_PROBE_SSH_HOST", ""),
        metavar="HOST",
        help="非空则优先 SSH 到此主机执行 vrf mtr（env MTR_PROBE_SSH_HOST；未设置则仅用本机探测）",
    )
    parser.add_argument(
        "--probe-ssh-user",
        default=os.environ.get("MTR_PROBE_SSH_USER", "root"),
    )
    parser.add_argument(
        "--probe-ssh-password",
        default=os.environ.get("MTR_PROBE_SSH_PASSWORD", "1234qwer"),
    )
    parser.add_argument(
        "--probe-vrf-exec",
        default=os.environ.get("MTR_PROBE_VRF_EXEC", "ip vrf exec vrf2102"),
        help="SSH 探测命令前缀（仅 SSH 模式）",
    )
    parser.add_argument(
        "--probe-local-vrf-exec",
        default=os.environ.get("MTR_PROBE_LOCAL_VRF_EXEC", ""),
        metavar="PREFIX",
        help="本机 mtr/traceroute 前加此前缀，例如：ip vrf exec vrf2103（env MTR_PROBE_LOCAL_VRF_EXEC）",
    )
    parser.add_argument(
        "--prefix-hop-ips",
        default=os.environ.get("MTR_HOP_PREFIX_IPS", ""),
        metavar="IPS",
        help="逗号分隔 IP，拼在本机/SSH 探测路径之前（env MTR_HOP_PREFIX_IPS）",
    )
    parser.add_argument(
        "--probe-mtr-extra",
        default=_default_probe_mtr_extra(),
        metavar="ARGS",
        help='插在 mtr `-r -n` 与 `-c` 之间，如 -4 -m 32 -I ens192 -a 10.133.152.204（env MTR_PROBE_MTR_EXTRA）',
    )
    parser.add_argument(
        "--probe-min-hops",
        type=int,
        default=_default_probe_min_hops(),
        metavar="N",
        help="mtr 解析跳数低于 N 时用 traceroute 再测一遍（env MTR_PROBE_MIN_HOPS；与合并探测配合）",
    )
    parser.add_argument(
        "--no-probe-merge-traceroute",
        action="store_true",
        help="关闭 mtr+traceroute 取长路径（默认开启；亦可用 env MTR_PROBE_MERGE_TRACEROUTE=0）",
    )
    parser.add_argument(
        "--sync-probe",
        action="store_true",
        help="同步实时探测：在 NFQUEUE 内现跑 mtr/traceroute，禁用后台 probe_loop；env MTR_PROBE_SYNC=1",
    )
    parser.add_argument(
        "--sync-probe-cache-sec",
        type=float,
        default=_default_sync_probe_cache_sec(),
        metavar="SEC",
        help="同步模式下同一目的复用 Hop 链秒数（默认 5；0=每包重跑；env MTR_PROBE_SYNC_CACHE_SEC）",
    )
    parser.add_argument(
        "--fallback-te-slots",
        type=int,
        default=int(os.environ.get("MTR_FALLBACK_TE_SLOTS", "48")),
        metavar="N",
        help="探测尚未写入前的占位 TE 槽位数，避免仅 1 跳导致路径塌缩（env MTR_FALLBACK_TE_SLOTS）",
    )
    parser.add_argument(
        "--cache-miss-action",
        default=os.environ.get("MTR_CACHE_MISS_ACTION", "accept"),
        choices=("accept", "fallback"),
        help="新目的尚无探测缓存时的行为：accept=放行并等待后台探测；fallback=使用占位 TE 链",
    )
    parser.add_argument(
        "--max-synthetic-hops",
        type=int,
        default=_default_max_synthetic_hops(),
        metavar="N",
        help="NFQUEUE 最多按 TTL 索引合成前 N 跳 TE（与探测链 merge 过长配合；env MTR_NFQ_MAX_SYNTHETIC_HOPS）",
    )
    script_dir = Path(__file__).resolve().parent
    default_db = script_dir.parent / "service" / "data.db"
    parser.add_argument(
        "--op-db",
        default=os.environ.get("MTR_OP_DB", str(default_db)),
        help="OP SQLite（hop_replace_rules）",
    )
    parser.add_argument("--rules-reload-sec", type=float, default=5.0)
    parser.add_argument(
        "--max-tracked-dsts",
        type=int,
        default=512,
        help="最多跟踪多少个 Echo 目的 IP（探测队列）；超出则丢弃最早记录",
    )
    args = parser.parse_args()

    probe_ssh_host: Optional[str] = None
    if not args.probe_local_only:
        h = (args.probe_ssh_host or "").strip()
        probe_ssh_host = h or None

    prefix_hop_ips = _parse_prefix_ips(args.prefix_hop_ips or "")
    probe_local_vrf_exec = (args.probe_local_vrf_exec or "").strip()
    probe_mtr_extra = (args.probe_mtr_extra or "").strip()
    probe_min_hops = max(2, min(64, int(args.probe_min_hops)))
    probe_merge_traceroute = _default_probe_merge_traceroute()
    if args.no_probe_merge_traceroute:
        probe_merge_traceroute = False

    sync_probe = bool(args.sync_probe) or _env_truthy("MTR_PROBE_SYNC", False)
    try:
        sync_cache_sec = max(0.0, float(args.sync_probe_cache_sec))
    except (TypeError, ValueError):
        sync_cache_sec = 0.0

    db_path = Path(args.op_db).expanduser()
    rules_cache = RuleCache(db_path, reload_sec=args.rules_reload_sec)
    cache_path = args.cache_file.strip() or None
    store = HopStore(cache_path)
    active_dsts = ActiveDstSet(max_items=args.max_tracked_dsts)
    rng = random.Random()
    stop_evt = threading.Event()
    wake_probe_evt = threading.Event()
    cache_miss_action = _cache_miss_action(args.cache_miss_action)

    sync_last: dict[str, tuple[float, list[HopEntry]]] = {}
    sync_locks_guard = threading.Lock()
    sync_dst_locks: dict[str, threading.Lock] = {}

    def _sync_lock_dst(d: str) -> threading.Lock:
        with sync_locks_guard:
            if d not in sync_dst_locks:
                sync_dst_locks[d] = threading.Lock()
            return sync_dst_locks[d]

    def refresh_hops_sync(dst_addr: str) -> Optional[list[HopEntry]]:
        lk = _sync_lock_dst(dst_addr)
        with lk:
            now = time.monotonic()
            if sync_cache_sec > 0 and dst_addr in sync_last:
                ts, hlist = sync_last[dst_addr]
                if now - ts < sync_cache_sec and hlist:
                    return list(hlist)
            rules = rules_cache.get()
            path = _probe_path_to_dst(
                dst_addr,
                timeout=max(5.0, args.probe_timeout),
                prefer_mtr=not args.no_mtr,
                probe_ssh_host=probe_ssh_host,
                probe_ssh_user=args.probe_ssh_user,
                probe_ssh_password=args.probe_ssh_password,
                probe_vrf_exec=args.probe_vrf_exec,
                probe_mtr_count=max(1, min(args.probe_mtr_count, 99)),
                probe_local_vrf_exec=probe_local_vrf_exec,
                mtr_extra=probe_mtr_extra,
                min_probe_hops=probe_min_hops,
                probe_merge_traceroute=probe_merge_traceroute,
                verbose=args.verbose,
            )
            raw_path = list(prefix_hop_ips) + path if prefix_hop_ips else path
            try:
                dip_s = str(ipaddress.IPv4Address(str(dst_addr).strip()))
                full_path = _finalize_probe_path(raw_path, dip_s)
            except ValueError:
                full_path = list(raw_path)
            hops, note = build_hops_from_probe(full_path, dst_addr, rules)
            sync_last[dst_addr] = (time.monotonic(), list(hops))
            store.set_dst(dst_addr, hops, full_path, note)
            if args.verbose:
                print(
                    f"sync_probe dst={dst_addr} note={note} hops={len(hops)} cache_ttl={sync_cache_sec}s",
                    flush=True,
                )
            return hops if hops else None

    if not sync_probe:
        t_probe = threading.Thread(
            target=probe_loop,
            kwargs=dict(
                store=store,
                rules_cache=rules_cache,
                active_dsts=active_dsts,
                interval=max(5.0, args.probe_interval),
                probe_timeout=max(5.0, args.probe_timeout),
                prefer_mtr=not args.no_mtr,
                probe_ssh_host=probe_ssh_host,
                probe_ssh_user=args.probe_ssh_user,
                probe_ssh_password=args.probe_ssh_password,
                probe_vrf_exec=args.probe_vrf_exec,
                probe_mtr_count=max(1, min(args.probe_mtr_count, 99)),
                probe_local_vrf_exec=probe_local_vrf_exec,
                probe_mtr_extra=probe_mtr_extra,
                probe_min_hops=probe_min_hops,
                probe_merge_traceroute=probe_merge_traceroute,
                prefix_hop_ips=prefix_hop_ips,
                rng=rng,
                verbose=args.verbose,
                wake=wake_probe_evt,
                stop=stop_evt,
            ),
            daemon=True,
            name="mtr-probe",
        )
        t_probe.start()

    try:
        from netfilterqueue import NetfilterQueue  # type: ignore
    except ImportError as e:
        print("需要 NetfilterQueue：pip install NetfilterQueue", file=sys.stderr)
        raise SystemExit(1) from e

    try:
        from scapy.all import ICMP, IP, Raw, send  # type: ignore
    except ImportError as e:
        print("需要 scapy", file=sys.stderr)
        raise SystemExit(1) from e

    static_fb = _long_fallback_te_chain(max(8, min(96, int(args.fallback_te_slots))))

    def handle(pkt) -> None:
        raw = pkt.get_payload()
        try:
            ip = IP(raw)
        except Exception:
            pkt.accept()
            return

        if ip.proto != socket.IPPROTO_ICMP:
            pkt.accept()
            return

        if ip.version != 4:
            pkt.accept()
            return

        icmp = ip.payload
        if not hasattr(icmp, "type") or icmp.type != 8:
            pkt.accept()
            return

        ttl = int(ip.ttl)
        dst = str(ip.dst)
        client = str(ip.src)
        if dst in local_ipv4_addresses():
            pkt.accept()
            return
        out_iface = _iface_for_client(client, args.host_201, args.host_202, args.iface_201, args.iface_202)

        if sync_probe:
            hops = refresh_hops_sync(dst)
        else:
            if active_dsts.add(dst):
                wake_probe_evt.set()
            hops = store.get_hops(dst)
        if not _has_non_fallback_hops(hops):
            if cache_miss_action == "fallback":
                hops = static_fb
            else:
                if args.verbose:
                    print(
                        f"CacheMissAccept ttl={ttl} dst={dst} client={client}",
                        flush=True,
                    )
                pkt.accept()
                return

        hop_index = max(0, ttl - 1)
        synth_cap = max(8, min(96, int(args.max_synthetic_hops)))
        chain_cap = min(len(hops), synth_cap)
        if hop_index >= chain_cap:
            if args.verbose:
                print(
                    f"AcceptCap ttl={ttl} idx={hop_index} chain_cap={chain_cap} len={len(hops)} "
                    f"dst={dst} client={client}",
                    flush=True,
                )
            pkt.accept()
            return

        if hop_index < len(hops):
            ent = hops[hop_index]
            _maybe_delay(ent, rng)
            inner = bytes(ip)[:28]
            out = (
                IP(src=ent.icmp_src, dst=client, ttl=ent.outbound_ttl)
                / ICMP(type=11, code=0)
                / Raw(load=inner)
            )
            if args.verbose:
                print(
                    f"TE ttl={ttl} idx={hop_index} src={ent.icmp_src} probed={ent.probed_ip} rule={ent.rule_id} client={client}",
                    flush=True,
                )
            send(out, iface=out_iface, verbose=0)
        else:
            if args.verbose:
                print(f"AcceptFinal ttl={ttl} dst={dst} client={client}", flush=True)
            pkt.accept()
            return

        pkt.drop()

    loc_warm = local_ipv4_addresses()
    print(
        f"mtr_spoof_nfqueue: warmup local_ipv4 count={len(loc_warm)} "
        f"has_152_200={'10.133.152.200' in loc_warm}",
        flush=True,
    )

    nfq = NetfilterQueue()
    nfq.bind(args.queue_num, handle)
    print(
        f"mtr_spoof_nfqueue: op_db={db_path} rules_reload={args.rules_reload_sec}s "
        f"probe_ssh={probe_ssh_host or 'OFF'} local_vrf={probe_local_vrf_exec or 'none'} "
        f"sync_probe={sync_probe} sync_cache_sec={sync_cache_sec} "
        f"max_synth_hops={args.max_synthetic_hops} "
        f"probe_merge_tt={probe_merge_traceroute} prefix_hops={len(prefix_hop_ips)} (running)",
        flush=True,
    )
    try:
        nfq.run()
    except KeyboardInterrupt:
        print("exit", flush=True)
    finally:
        stop_evt.set()
        wake_probe_evt.set()


if __name__ == "__main__":
    main()
