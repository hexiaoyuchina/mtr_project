"""上游 BGP 前缀持久缓存；RR/上游断连时经 GoBGP TX 向下游继续通告（blackhole + TX AddPath）。"""
from __future__ import annotations

import ipaddress
import logging
import os
import platform
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Set

from . import bgp_control, storage, vpn_egress

logger = logging.getLogger(__name__)


def sticky_advert_enabled() -> bool:
    raw = (os.environ.get("MTR_BGP_STICKY_ADVERT") or "").strip().lower()
    if raw in {"0", "false", "no"}:
        return False
    if raw in {"1", "true", "yes"}:
        return True
    return platform.system() == "Linux"


def upstream_cache_learn_vrf() -> str:
    return storage.validate_vrf_name(
        (os.environ.get("MTR_BGP_UPSTREAM_CACHE_VRF") or bgp_control.GOBGP_VRF_RR).strip()
    )


def sticky_advert_vrf() -> str:
    return storage.validate_vrf_name(
        (os.environ.get("MTR_BGP_STICKY_ADVERT_VRF") or "default").strip()
    )


def _first_upstream_neighbor_ip(conn: sqlite3.Connection, learn_vrf: str) -> str:
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
    env = bgp_control.agent_env_config()
    return env["rr_addr"]


def first_upstream_neighbor_ip(conn: sqlite3.Connection, learn_vrf: str) -> str:
    return _first_upstream_neighbor_ip(conn, learn_vrf)


def _normalize_prefix(p: str) -> str:
    return str(ipaddress.ip_network(p.strip(), strict=False))


def _ip_blackhole(vrf: str, prefix: str, add: bool) -> tuple[int, str, str]:
    pfx = _normalize_prefix(prefix)
    v = storage.validate_vrf_name(vrf)
    if add:
        return vpn_egress._ip(["route", "replace", "blackhole", pfx, "vrf", v], timeout=30)
    return vpn_egress._ip(["route", "del", pfx, "vrf", v], timeout=30)


def _sticky_kernel_apply_ok() -> bool:
    if platform.system() != "Linux":
        return False
    if os.environ.get("MTR_BGP_STICKY_APPLY_KERNEL", "1").strip().lower() in {"0", "false", "no"}:
        return False
    return True


def _upstream_established(conn: sqlite3.Connection, learn_vrf: str, cached_rows: list) -> bool:
    up_ip = first_upstream_neighbor_ip(conn, learn_vrf)
    if up_ip:
        if up_ip == bgp_control.agent_env_config()["rr_addr"]:
            return bgp_control.rr_is_established()
        return bgp_control.neighbor_is_established(learn_vrf, up_ip)
    if not cached_rows:
        return True
    cache_peers = sorted(
        {str(r["neighbor_ip"] or "").strip() for r in cached_rows if str(r.get("neighbor_ip") or "").strip()}
    )
    env_rr = bgp_control.agent_env_config()["rr_addr"]
    for nip in cache_peers:
        if nip == env_rr and bgp_control.rr_is_established():
            return True
        if bgp_control.neighbor_is_established(learn_vrf, nip):
            return True
    return False


def reconcile_sticky_for_downstream(conn: sqlite3.Connection) -> Dict[str, object]:
    out: Dict[str, object] = {
        "enabled": False,
        "upstream_established": None,
        "desired": 0,
        "added": 0,
        "removed": 0,
        "errors": [],
    }
    if not sticky_advert_enabled():
        logger.info("bgp sticky: disabled")
        return out
    if not _sticky_kernel_apply_ok():
        out["errors"].append("sticky_kernel_apply_disabled_or_non_linux")
        return out
    if not bgp_control.health_ok():
        out["errors"].append("bgp_agent_unavailable")
        return out

    learn_vrf = upstream_cache_learn_vrf()
    advert_vrf = sticky_advert_vrf()
    cached_rows = storage.list_bgp_upstream_cache_rows(conn, learn_vrf)
    cached_prefixes: Set[str] = {str(r["prefix"]) for r in cached_rows}
    up_ip = first_upstream_neighbor_ip(conn, learn_vrf)
    cache_peer_hint = ""
    for r in sorted(cached_rows, key=lambda x: str(x["prefix"] or "")):
        nip = str(r["neighbor_ip"] or "").strip()
        if nip:
            cache_peer_hint = nip
            break

    established = _upstream_established(conn, learn_vrf, cached_rows)
    out["enabled"] = True
    out["upstream_established"] = established
    out["upstream_neighbor"] = up_ip or cache_peer_hint or ""
    out["upstream_meta_missing"] = bool(not up_ip and cached_prefixes)

    desired: Set[str] = set() if established else set(cached_prefixes)
    installed = set(storage.list_bgp_sticky_frr_prefixes(conn, advert_vrf))
    to_add = sorted(desired - installed)
    to_remove = sorted(installed - desired)
    ts = datetime.utcnow().isoformat() + "Z"
    nh = bgp_control.default_router_id()

    for pfx in to_add:
        try:
            rc, _o, err = _ip_blackhole(advert_vrf, pfx, True)
            if rc != 0:
                raise RuntimeError(f"ip_route_blackhole_failed rc={rc}: {err}")
            bgp_control.set_bgp_ipv4_network(advert_vrf, pfx, True, nexthop=nh)
            storage.add_bgp_sticky_frr(conn, advert_vrf, pfx, ts)
            out["added"] = int(out["added"]) + 1  # type: ignore[arg-type]
            logger.info("bgp sticky: tx advertise %s vrf=%s", pfx, advert_vrf)
        except Exception as e:
            msg = f"add {pfx}: {e}"
            logger.warning("bgp sticky: %s", msg)
            out["errors"].append(msg)
            try:
                vpn_egress._ip(
                    ["route", "del", _normalize_prefix(pfx), "vrf", storage.validate_vrf_name(advert_vrf)],
                    timeout=20,
                )
            except Exception:
                pass

    for pfx in to_remove:
        try:
            bgp_control.set_bgp_ipv4_network(advert_vrf, pfx, False)
        except Exception as e:
            msg = f"withdraw {pfx}: {e}"
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
