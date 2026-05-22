"""nftables：hijack_enabled 开启时，按 hop 规则写入 table ip mtr_te_snat 占位 SNAT（table inet mtr_te 为空滤镜占位）。

注意（实验室实测）：Linux 5.4 + VRF 下，**转发的 ICMP Time Exceeded 通常不进 nat POSTROUTING**，
nft 计数恒为 0；真实改写由 **iptables mangle FORWARD → NFQUEUE** + **`te_rewrite_nfqueue.py`**
完成（见 `tools/deploy_light.py`）。此处 SNAT 规则可作占位或与将来内核行为兼容。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from .hop_cidr import iter_ipv4_addresses

logger = logging.getLogger(__name__)


def _nft(args: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(["nft", *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def load_table_from_file(nft_file: Path) -> None:
    if not nft_file.is_file():
        raise FileNotFoundError(nft_file)
    code, out, err = _nft(["-f", str(nft_file)])
    if code != 0:
        raise RuntimeError(f"nft -f failed: {err or out}")


def ensure_table(nft_file: Path) -> None:
    """加载 inet mtr_te + ip mtr_te_snat；兼容旧表名，先删后载。"""
    for fam, name in (
        ("inet", "mtr_spoof"),
        ("inet", "mtr_te"),
        ("inet", "mtr_te_snat"),
        ("ip", "mtr_te_snat"),
    ):
        code, _, _ = _nft(["delete", "table", fam, name])
        if code != 0:
            logger.debug("nft delete table %s %s (ok if missing)", fam, name)
    load_table_from_file(nft_file)


def _forge_ipv4(s: str) -> str:
    a = ipaddress.IPv4Address(s.strip())
    return str(a)


def _match_ipv4_cidr_nft(match_cidr: str) -> str:
    """返回 nft `ip saddr` 可用的前缀字符串（RFC 对齐网段）。"""
    net = ipaddress.ip_network(match_cidr.strip(), strict=False)
    if net.version != 4:
        raise ValueError("IPv6 not supported for TE SNAT")
    return str(net)


def _nft_downstream_iface() -> str:
    for key in (
        "MTR_NFT_TE_SNAT_OIF",
        "MTR_TE_REWRITE_OIF",
        "MTR_BGP_IPVLAN_BASE_IFACE",
        "MTR_OP_DOWNSTREAM_IFACE",
    ):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    return "ens192"


def _nft_uplink_iface() -> str:
    for key in ("MTR_NFT_TE_SNAT_IIF", "MTR_TE_REWRITE_IIF", "MTR_BGP_RR_UPLINK_IFACE"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    return "ens224"


def _extra_matches(include_vrf: bool) -> list[str]:
    """环境变量附加匹配。"""
    extra: list[str] = []
    oif = (os.environ.get("MTR_NFT_TE_SNAT_OIF") or "").strip() or _nft_downstream_iface()
    if oif:
        extra.extend(["oifname", oif])
    iif = (os.environ.get("MTR_NFT_TE_SNAT_IIF") or "").strip() or _nft_uplink_iface()
    if iif:
        extra.extend(["iifname", iif])
    vrf = (os.environ.get("MTR_NFT_TE_SNAT_VRF") or "").strip()
    if vrf and include_vrf:
        extra.extend(["meta", "vrf", "name", vrf])
    return extra


def _extra_match_trials() -> list[list[str]]:
    """先试带 vrf meta（若配置了），再试不含 vrf。"""
    out: list[list[str]] = []
    if (os.environ.get("MTR_NFT_TE_SNAT_VRF") or "").strip():
        out.append(_extra_matches(include_vrf=True))
    plain = _extra_matches(include_vrf=False)
    if not out or out[-1] != plain:
        out.append(plain)
    return out


def add_te_snat_rules(hop_rules: list[dict[str, Any]]) -> None:
    """按数据库顺序（priority DESC）追加 SNAT 规则（table ip）。

    match_cidr 与 te_rewrite 一致：「起始 IPv4 + /前缀」表示连续地址段；
    段内地址展开为多条 ``ip saddr <host>``（上限见 MTR_NFT_TE_SNAT_EXPAND_PER_RULE），
    过大则退回 RFC 对齐单条前缀。
    """
    max_rules = int(os.environ.get("MTR_NFT_TE_SNAT_MAX", "512"))
    expand_max = int(os.environ.get("MTR_NFT_TE_SNAT_EXPAND_PER_RULE", "256"))
    trials = _extra_match_trials()
    if any(trials):
        logger.info("nft TE SNAT match trials: %s", trials)
    n = 0
    for r in hop_rules:
        if n >= max_rules:
            logger.warning("nft TE SNAT: truncated at %s rules", max_rules)
            break
        rid = r.id
        mc = str(r.match_cidr or "")
        try:
            forged = _forge_ipv4(str(r.forged_src or ""))
        except (ValueError, ipaddress.AddressValueError) as e:
            logger.warning("skip hop rule id=%s: %s", rid, e)
            continue

        targets: list[str]
        try:
            targets = [str(a) for a in iter_ipv4_addresses(mc, max_addresses=expand_max)]
        except ValueError as e:
            logger.warning(
                "nft TE SNAT id=%s cannot expand span (%s), use canonical CIDR",
                rid,
                e,
            )
            try:
                targets = [_match_ipv4_cidr_nft(mc)]
            except (ValueError, ipaddress.AddressValueError) as ex:
                logger.warning("skip hop rule id=%s: %s", rid, ex)
                continue

        for cidr_s in targets:
            if n >= max_rules:
                logger.warning("nft TE SNAT: truncated at %s rules", max_rules)
                break
            base = [
                "add",
                "rule",
                "ip",
                "mtr_te_snat",
                "postrouting",
                "ip",
                "saddr",
                cidr_s,
                "icmp",
                "type",
                "time-exceeded",
            ]
            tail = ["counter", "snat", "to", forged]

            ok = False
            for idx, extra in enumerate(trials):
                args = base + extra + tail
                code, _, err = _nft(args)
                if code == 0:
                    ok = True
                    break
                if idx == 0 and len(trials) > 1:
                    logger.warning(
                        "nft TE SNAT rule id=%s attempt failed: %s — retry next trial",
                        rid,
                        err.strip(),
                    )
                    continue
                logger.error("nft TE SNAT rule id=%s failed: %s", rid, err.strip())
            if not ok:
                continue

            n += 1
    logger.info("nft TE SNAT: installed %s rules", n)


def _ensure_inet_mtr_te_table(nft_file: Path) -> None:
    code, _, _ = _nft(["list", "table", "inet", "mtr_te"])
    if code == 0:
        return
    if not nft_file.is_file():
        raise FileNotFoundError(nft_file)
    load_table_from_file(nft_file)


def _ensure_ip_te_snat_table(nft_file: Path) -> None:
    code, _, _ = _nft(["list", "table", "ip", "mtr_te_snat"])
    if code == 0:
        return
    if not nft_file.is_file():
        raise FileNotFoundError(nft_file)
    load_table_from_file(nft_file)


def sync_te_snat_only(
    *,
    nft_file: Path,
    hijack_enabled: bool,
    hop_rules: list[dict[str, Any]] | None = None,
) -> None:
    """仅刷新 ip mtr_te_snat postrouting，不 delete inet mtr_te（缩短 hop 规则变更影响）。"""
    _ensure_inet_mtr_te_table(nft_file)
    _ensure_ip_te_snat_table(nft_file)
    _nft(["flush", "chain", "ip", "mtr_te_snat", "postrouting"])
    if not hijack_enabled:
        logger.info("nft TE SNAT cleared (hijack off)")
        return
    add_te_snat_rules(list(hop_rules or []))
    logger.info(
        "nft TE SNAT only: %s enabled rules",
        len(hop_rules or []),
    )


def sync_nft(
    *,
    nft_file: Path,
    hijack_enabled: bool,
    hop_rules: list[dict[str, Any]] | None = None,
) -> None:
    """
    hijack_enabled=False → 仅重载空表（无 TE SNAT）。
    hijack_enabled=True → 按 hop_replace_rules 写入「ICMP TE → snat to forged_src」。
    """
    ensure_table(nft_file)
    if not hijack_enabled:
        logger.info("nft hijack off (no ICMP TE SNAT)")
        return
    add_te_snat_rules(list(hop_rules or []))
    logger.info(
        "nft hijack on (ICMP time-exceeded SNAT, %s enabled rules)",
        len(hop_rules or []),
    )
