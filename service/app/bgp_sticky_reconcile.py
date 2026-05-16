"""上游 BGP 前缀持久缓存；ROS 断连时在下游 VRF 注入 blackhole + BGP network，供 Linux 201 仍收到路由。"""
from __future__ import annotations

import ipaddress
import logging
import os
import platform
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Set

from . import frr_bgp, storage, vpn_egress

logger = logging.getLogger(__name__)


def sticky_advert_enabled() -> bool:
    """
    是否向下游 VRF 注入 blackhole + BGP network。

    未设置环境变量时：**Linux 默认开启**（现场 ROS 断连后仍对 Linux 201 通告缓存前缀）；
    显式 ``MTR_BGP_STICKY_ADVERT=0`` / ``false`` / ``no`` 关闭。
    """
    raw = (os.environ.get("MTR_BGP_STICKY_ADVERT") or "").strip().lower()
    if raw in {"0", "false", "no"}:
        return False
    if raw in {"1", "true", "yes"}:
        return True
    return platform.system() == "Linux"


def upstream_cache_learn_vrf() -> str:
    return storage.validate_vrf_name((os.environ.get("MTR_BGP_UPSTREAM_CACHE_VRF") or "vrf2103").strip())


def sticky_advert_vrf() -> str:
    return storage.validate_vrf_name((os.environ.get("MTR_BGP_STICKY_ADVERT_VRF") or "vrf2102").strip())


def _first_upstream_neighbor_ip(conn: sqlite3.Connection, learn_vrf: str) -> str:
    # 优先「BGP 管理」写入的 bgp_neighbor_meta（role=upstream）；无录入时再回退环境变量（自动化场景）。
    meta = storage.get_bgp_neighbor_meta_map(conn, learn_vrf)
    upstream_ips = sorted(
        ip for ip, row in meta.items() if (str(row[0] or "").strip().lower() == "upstream")
    )
    if upstream_ips:
        return upstream_ips[0]
    env_ip = (os.environ.get("MTR_BGP_STICKY_UPSTREAM_NEIGHBOR") or "").strip()
    if env_ip:
        try:
            return storage.validate_ipv4(env_ip)
        except ValueError:
            pass
    return ""


def first_upstream_neighbor_ip(conn: sqlite3.Connection, learn_vrf: str) -> str:
    """解析上游邻居 IP：优先 SQLite ``bgp_neighbor_meta`` 中 role=upstream（OP「BGP 管理」录入）；若无则回退 ``MTR_BGP_STICKY_UPSTREAM_NEIGHBOR``。"""
    return _first_upstream_neighbor_ip(conn, learn_vrf)


def _normalize_prefix(p: str) -> str:
    return str(ipaddress.ip_network(p.strip(), strict=False))


def _ip_blackhole(vrf: str, prefix: str, add: bool) -> tuple[int, str, str]:
    pfx = _normalize_prefix(prefix)
    v = storage.validate_vrf_name(vrf)
    # 现场 Linux 200（较旧 iproute2）：``ip route replace blackhole PREFIX vrf NAME`` 才合法；
    # ``PREFIX vrf … blackhole`` 会报「Command line is not complete」；``… blackhole vrf …`` 会报 vrf is garbage。
    if add:
        return vpn_egress._ip(["route", "replace", "blackhole", pfx, "vrf", v], timeout=30)
    return vpn_egress._ip(["route", "del", pfx, "vrf", v], timeout=30)


def _sticky_kernel_apply_ok() -> bool:
    if platform.system() != "Linux":
        return False
    if os.environ.get("MTR_BGP_STICKY_APPLY_KERNEL", "1").strip().lower() in {"0", "false", "no"}:
        return False
    return True


def reconcile_sticky_for_downstream(conn: sqlite3.Connection) -> Dict[str, object]:
    """
    若上游邻居非 Established：把 ``bgp_upstream_route_cache`` 中前缀在 ``sticky_advert_vrf()`` 下
    安装 blackhole + ``network``；恢复 Established 后拆除 OP 曾写入的项。

    若 OP 已删除 ``bgp_neighbor_meta`` 中的 upstream 但缓存表仍有前缀，则根据缓存里的 ``neighbor_ip``
    在 FRR 中是否 Established 判定；若均非 Established，仍向下游通告（避免删邻居后 201 收不到缓存路由）。
    """
    out: Dict[str, object] = {"enabled": False, "upstream_established": None, "desired": 0, "added": 0, "removed": 0, "errors": []}
    if not sticky_advert_enabled():
        logger.info("bgp sticky: disabled (export MTR_BGP_STICKY_ADVERT=1 on non-Linux; Linux default is on unless set to 0)")
        return out
    if not _sticky_kernel_apply_ok():
        out["errors"].append("sticky_kernel_apply_disabled_or_non_linux")
        return out
    learn_vrf = upstream_cache_learn_vrf()
    advert_vrf = sticky_advert_vrf()
    sd: Dict[str, set[str]] = {}
    try:
        sd = frr_bgp.neighbor_shutdown_by_vrf_from_running_config()
    except frr_bgp.VtyshError:
        pass

    cached_rows = storage.list_bgp_upstream_cache_rows(conn, learn_vrf)
    cached_prefixes: Set[str] = {str(r["prefix"]) for r in cached_rows}
    up_ip = first_upstream_neighbor_ip(conn, learn_vrf)
    # 缓存行里的 neighbor_ip 仅作摘要展示（OP 删除邻居时 meta 可能已空，但表 bgp_upstream_route_cache 仍有前缀）
    cache_peer_hint = ""
    for r in sorted(cached_rows, key=lambda x: str(x["prefix"] or "")):
        nip = str(r["neighbor_ip"] or "").strip()
        if nip:
            cache_peer_hint = nip
            break

    if up_ip:
        established = frr_bgp.neighbor_is_established(learn_vrf, up_ip, sd)
    elif cached_prefixes:
        cache_peers = sorted(
            {str(r["neighbor_ip"] or "").strip() for r in cached_rows if str(r.get("neighbor_ip") or "").strip()}
        )
        any_cache_peer_up = any(
            frr_bgp.neighbor_is_established(learn_vrf, nip, sd) for nip in cache_peers
        )
        if any_cache_peer_up:
            established = True
            logger.info(
                "bgp sticky: learn_vrf=%s no upstream meta/env; cache peer(s) Established — skip sticky advert",
                learn_vrf,
            )
        else:
            established = False
            logger.info(
                "bgp sticky: learn_vrf=%s no upstream meta/env and no cache peer Established — advertising %s sticky prefix(es)",
                learn_vrf,
                len(cached_prefixes),
            )
    else:
        out["errors"].append("no_upstream_neighbor_skip_sticky_advert")
        established = True

    out["enabled"] = True
    out["upstream_established"] = established
    out["upstream_neighbor"] = up_ip or cache_peer_hint or ""
    out["upstream_meta_missing"] = bool(not up_ip and cached_prefixes)

    desired: Set[str] = set() if established else set(cached_prefixes)

    installed = set(storage.list_bgp_sticky_frr_prefixes(conn, advert_vrf))
    to_add = sorted(desired - installed)
    to_remove = sorted(installed - desired)
    ts = datetime.utcnow().isoformat() + "Z"

    for pfx in to_add:
        try:
            rc, _o, err = _ip_blackhole(advert_vrf, pfx, True)
            if rc != 0:
                raise RuntimeError(f"ip_route_blackhole_failed rc={rc}: {err}")
            frr_bgp.set_bgp_ipv4_network(advert_vrf, pfx, True)
            storage.add_bgp_sticky_frr(conn, advert_vrf, pfx, ts)
            out["added"] = int(out["added"]) + 1  # type: ignore[arg-type]
            logger.info("bgp sticky: installed %s vrf=%s", pfx, advert_vrf)
        except Exception as e:
            msg = f"add {pfx}: {e}"
            logger.warning("bgp sticky: %s", msg)
            out["errors"].append(msg)
            try:
                vpn_egress._ip(["route", "del", _normalize_prefix(pfx), "vrf", storage.validate_vrf_name(advert_vrf)], timeout=20)
            except Exception:
                pass

    for pfx in to_remove:
        try:
            frr_bgp.set_bgp_ipv4_network(advert_vrf, pfx, False)
        except Exception as e:
            msg = f"no network {pfx}: {e}"
            logger.warning("bgp sticky: %s", msg)
            out["errors"].append(msg)
        try:
            _ip_blackhole(advert_vrf, pfx, False)
        except Exception:
            pass
        storage.remove_bgp_sticky_frr(conn, advert_vrf, pfx)
        out["removed"] = int(out["removed"]) + 1  # type: ignore[arg-type]
        logger.info("bgp sticky: removed %s vrf=%s", pfx, advert_vrf)

    out["desired"] = len(desired)
    logger.info(
        "bgp sticky: done upstream=%s established=%s desired=%s added=%s removed=%s err=%s",
        up_ip or "-",
        established,
        out["desired"],
        out["added"],
        out["removed"],
        len(out["errors"]),
    )
    return out


def maybe_prune_upstream_cache(conn: sqlite3.Connection, learn_vrf: str) -> int:
    raw = (os.environ.get("MTR_BGP_UPSTREAM_CACHE_PRUNE_SEC") or "0").strip()
    try:
        sec = int(raw)
    except ValueError:
        sec = 0
    if sec <= 0:
        return 0
    cutoff = (datetime.utcnow() - timedelta(seconds=sec)).isoformat() + "Z"
    return storage.prune_bgp_upstream_route_cache_before(conn, learn_vrf, cutoff)


def merge_stale_upstream_into_routes(
    conn: sqlite3.Connection,
    learn_vrf: str,
    live_upstream_prefixes: Set[str],
) -> List[Dict[str, object]]:
    """返回需追加到 API 的「仅缓存、当前上游 RIB 快照无」的前缀行（stale=True）。"""
    out: List[Dict[str, object]] = []
    for r in storage.list_bgp_upstream_cache_rows(conn, learn_vrf):
        pfx = str(r["prefix"])
        if pfx in live_upstream_prefixes:
            continue
        out.append(
            {
                "vrf": learn_vrf,
                "prefix": pfx,
                "nexthop": str(r["nexthop"] or ""),
                "neighbor_ip": str(r["neighbor_ip"] or ""),
                "remote_as": int(r["remote_as"] or 0),
                "role": "upstream",
                "as_path": str(r["as_path"] or ""),
                "updated_at": str(r["last_live_at"] or ""),
                "stale": True,
            }
        )
    return out
