"""从 FRR 拉取 BGP IPv4 单播 RIB，写入 SQLite；上游前缀另写入持久缓存供断连时向下游通告。"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Tuple

from . import bgp_sticky_reconcile, frr_bgp, storage
from .bgp_route_cache import get_global_cache

logger = logging.getLogger(__name__)

# 线程本地存储，每个线程有独立的数据库连接
_thread_local = threading.local()


def _get_thread_db_conn(db_path: Path):
    """获取当前线程的数据库连接，线程安全"""
    if not hasattr(_thread_local, 'conn'):
        _thread_local.conn = storage.connect(db_path)
        storage.init_schema(_thread_local.conn)
    return _thread_local.conn


def _close_thread_db_conn():
    """关闭当前线程的数据库连接"""
    if hasattr(_thread_local, 'conn'):
        try:
            _thread_local.conn.close()
        except Exception:
            pass
        delattr(_thread_local, 'conn')


def _process_neighbor_routes(
    db_path: Path,
    vrf: str,
    neighbor_ip: str,
    neighbor_info: frr_bgp.BgpNeighborSummary,
    cache_vrf: str,
    upstream_ips: set[str],
    ts: str,
    meta: Dict[str, Tuple[str, str]],
    result_dict: Dict[str, Any],
    lock: threading.Lock,
):
    """
    单个 BGP neighbor 的路由处理线程函数。
    每个 neighbor 独立线程处理路由数据存储。
    """
    logger.info(f"Processing neighbor {neighbor_ip} in vrf {vrf}")
    thread_id = threading.current_thread().ident
    try:
        conn = _get_thread_db_conn(db_path)
        
        # 获取该 neighbor 的路由
        rows: List[tuple] = []
        try:
            rib = frr_bgp.list_bgp_ipv4_unicast_rib(vrf)
        except frr_bgp.VtyshError as e:
            logger.warning(f"Thread {thread_id}: bgp rib read vrf={vrf} neighbor={neighbor_ip} failed: {e}")
            return
        
        for e in rib:
            nh = (e.nexthop or "").strip()
            peer_ip = ""
            ra = 0
            if nh == neighbor_ip:
                peer_ip = nh
                ra = int(neighbor_info.remote_as)
            else:
                ph = (e.peer_hint or "").strip()
                if ph == neighbor_ip:
                    peer_ip = ph
                    ra = int(neighbor_info.remote_as)
            
            if not peer_ip:
                continue
            
            gl_peer, gl_role = storage.lookup_bgp_neighbor_meta_for_nexthop(conn, vrf, nh) if nh else ("", "unknown")
            if not peer_ip and gl_peer:
                peer_ip = gl_peer
            if not ra:
                ra = first_asn_from_path(e.as_path)
            
            role_t = "unknown"
            if peer_ip and peer_ip in meta:
                rp = str(meta[peer_ip][0] or "unknown").strip().lower()
                if rp in storage.BGP_META_ROLES:
                    role_t = rp
            gr = str(gl_role or "unknown").strip().lower()
            if role_t == "unknown" and gr in storage.BGP_META_ROLES and gr != "unknown":
                role_t = gr
            
            ap = (e.as_path or "")[:512]
            rows.append((e.prefix, nh, peer_ip or "", ra, role_t, ap, ts))
        
        # 处理上游缓存
        upstream_cache_rows: List[tuple] = []
        if vrf == cache_vrf:
            # 从现有快照中提取该 neighbor 的上游路由
            for r in storage.list_bgp_learned_routes(conn, vrf, neighbor_ip):
                role_o = str(r["role"] or "").strip().lower()
                nh_o = str(r["nexthop"] or "").strip()
                peer_o = str(r["neighbor_ip"] or "").strip()
                if role_o == "upstream" or nh_o in upstream_ips or peer_o in upstream_ips:
                    upstream_cache_rows.append(
                        (vrf, str(r["prefix"]), nh_o, peer_o, int(r["remote_as"] or 0), str(r["as_path"] or "")[:512], ts)
                    )
            
            # 添加当前轮的上游路由
            for tup in rows:
                if len(tup) < 7:
                    continue
                role_t = str(tup[4] or "").strip().lower()
                nh, peer = str(tup[1] or "").strip(), str(tup[2] or "").strip()
                if role_t == "upstream" or nh in upstream_ips or peer in upstream_ips:
                    upstream_cache_rows.append((vrf, tup[0], nh, peer, int(tup[3]), tup[5], ts))
        
        # 线程安全地收集结果
        with lock:
            result_dict[(vrf, neighbor_ip)] = {
                'rows': rows,
                'upstream_cache_rows': upstream_cache_rows,
                'success': True
            }
        
        logger.info(f"Thread {thread_id}: Completed processing neighbor {neighbor_ip} in vrf {vrf}, {len(rows)} routes")
        
    except Exception as e:
        logger.exception(f"Thread {thread_id}: Error processing neighbor {neighbor_ip} in vrf {vrf}")
        with lock:
            result_dict[(vrf, neighbor_ip)] = {
                'success': False,
                'error': str(e)
            }


def first_asn_from_path(path: str) -> int:
    for tok in (path or "").replace("{", " ").replace("}", " ").replace(",", " ").split():
        t = tok.strip()
        if t.isdigit():
            v = int(t)
            if 1 <= v <= 4294967295:
                return v
    return 0


def sync_bgp_learned_routes(db_path: Path, use_threading: bool = True) -> Dict[str, Any]:
    """
    从 FRR 拉取 BGP IPv4 单播 RIB，写入 SQLite。
    
    :param db_path: SQLite 数据库路径
    :param use_threading: 是否使用多线程模式，每个 BGP session 独立线程处理路由数据
    """
    conn = storage.connect(db_path)
    sticky_summary: Dict[str, Any] = {}
    try:
        storage.init_schema(conn)
        ts = datetime.utcnow().isoformat() + "Z"
        cache_vrf = bgp_sticky_reconcile.upstream_cache_learn_vrf()
        insts = frr_bgp.list_bgp_instances()
        try:
            sd = frr_bgp.neighbor_shutdown_by_vrf_from_running_config()
        except frr_bgp.VtyshError:
            sd = {}
        active_vrfs: set[str] = set()
        
        if use_threading:
            # 多线程模式：每个 BGP neighbor 独立线程处理
            sticky_summary = _sync_bgp_learned_routes_multi_thread(db_path, cache_vrf, insts, sd, ts, active_vrfs)
        else:
            # 单线程模式：原有逻辑
            sticky_summary = _sync_bgp_learned_routes_single_thread(conn, cache_vrf, insts, sd, ts, active_vrfs)
        
        # 清理过期 VRF 数据
        storage.delete_bgp_learned_routes_not_in_vrfs(conn, active_vrfs)
        try:
            bgp_sticky_reconcile.maybe_prune_upstream_cache(conn, cache_vrf)
        except Exception:
            logger.exception("bgp upstream cache prune")
        try:
            sticky_summary.update(bgp_sticky_reconcile.reconcile_sticky_for_downstream(conn))
        except Exception:
            logger.exception("bgp sticky reconcile")
        storage.set_bgp_rib_sync_state(conn, ts, True, "")
        
    except Exception as e:
        logger.exception("sync_bgp_learned_routes")
        try:
            storage.set_bgp_rib_sync_state(conn, datetime.utcnow().isoformat() + "Z", False, str(e))
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return sticky_summary


def _sync_bgp_learned_routes_single_thread(
    conn,
    cache_vrf: str,
    insts,
    sd: Dict[str, set[str]],
    ts: str,
    active_vrfs: set[str],
) -> Dict[str, Any]:
    """单线程模式同步 BGP 学习路由"""
    sticky_summary: Dict[str, Any] = {}
    
    for inst in insts:
        vrf = storage.validate_vrf_name(inst.vrf)
        active_vrfs.add(vrf)
        try:
            rib = frr_bgp.list_bgp_ipv4_unicast_rib(vrf)
        except frr_bgp.VtyshError as e:
            logger.warning("bgp rib read vrf=%s failed, keeping last db snapshot: %s", vrf, e)
            continue
        neighbors = {n.ip: n for n in frr_bgp.list_bgp_neighbors(vrf, sd)}
        meta = storage.get_bgp_neighbor_meta_map(conn, vrf)
        upstream_ips = {
            nip
            for nip, tup in meta.items()
            if str((tup[0] if tup else "") or "").strip().lower() == "upstream"
        }
        rows: List[tuple] = []
        for e in rib:
            nh = (e.nexthop or "").strip()
            peer_ip = ""
            ra = 0
            if nh in neighbors:
                peer_ip = nh
                ra = int(neighbors[nh].remote_as)
            else:
                ph = (e.peer_hint or "").strip()
                if ph in neighbors:
                    peer_ip = ph
                    ra = int(neighbors[ph].remote_as)
            gl_peer, gl_role = storage.lookup_bgp_neighbor_meta_for_nexthop(conn, vrf, nh) if nh else ("", "unknown")
            if not peer_ip and gl_peer:
                peer_ip = gl_peer
            if not ra:
                ra = first_asn_from_path(e.as_path)
            role_t = "unknown"
            if peer_ip and peer_ip in meta:
                rp = str(meta[peer_ip][0] or "unknown").strip().lower()
                if rp in storage.BGP_META_ROLES:
                    role_t = rp
            gr = str(gl_role or "unknown").strip().lower()
            if role_t == "unknown" and gr in storage.BGP_META_ROLES and gr != "unknown":
                role_t = gr
            ap = (e.as_path or "")[:512]
            rows.append((e.prefix, nh, peer_ip or "", ra, role_t, ap, ts))
        
        _process_vrf_routes(conn, vrf, cache_vrf, rows, upstream_ips, ts, sd)
    
    return sticky_summary


def _sync_bgp_learned_routes_multi_thread(
    db_path: Path,
    cache_vrf: str,
    insts,
    sd: Dict[str, set[str]],
    ts: str,
    active_vrfs: set[str],
) -> Dict[str, Any]:
    """多线程模式同步 BGP 学习路由，每个 neighbor 独立线程处理"""
    sticky_summary: Dict[str, Any] = {}
    threads = []
    result_dict: Dict[str, Any] = {}
    lock = threading.Lock()
    
    for inst in insts:
        vrf = storage.validate_vrf_name(inst.vrf)
        active_vrfs.add(vrf)
        
        try:
            neighbors = {n.ip: n for n in frr_bgp.list_bgp_neighbors(vrf, sd)}
        except frr_bgp.VtyshError as e:
            logger.warning("bgp neighbors read vrf=%s failed: %s", vrf, e)
            continue
        
        if not neighbors:
            continue
        
        # 获取 VRF 的元数据
        meta_conn = storage.connect(db_path)
        try:
            meta = storage.get_bgp_neighbor_meta_map(meta_conn, vrf)
            upstream_ips = {
                nip
                for nip, tup in meta.items()
                if str((tup[0] if tup else "") or "").strip().lower() == "upstream"
            }
        finally:
            meta_conn.close()
        
        # 为每个 neighbor 创建独立线程
        for neighbor_ip, neighbor_info in neighbors.items():
            if not neighbor_info.enabled:
                continue
            
            thread = threading.Thread(
                target=_process_neighbor_routes,
                args=(
                    db_path,
                    vrf,
                    neighbor_ip,
                    neighbor_info,
                    cache_vrf,
                    upstream_ips,
                    ts,
                    meta,
                    result_dict,
                    lock,
                ),
                name=f"BGP-{vrf}-{neighbor_ip}"
            )
            threads.append(thread)
            thread.start()
    
    # 等待所有线程完成
    for thread in threads:
        thread.join()
    
    # 合并所有线程的结果并写入数据库
    conn = storage.connect(db_path)
    try:
        # 按 VRF 分组结果
        vrf_results: Dict[str, Dict[str, Any]] = {}
        for (vrf, neighbor_ip), result in result_dict.items():
            if not result.get('success'):
                continue
            if vrf not in vrf_results:
                vrf_results[vrf] = {
                    'rows': [],
                    'upstream_cache_rows': []
                }
            vrf_results[vrf]['rows'].extend(result.get('rows', []))
            vrf_results[vrf]['upstream_cache_rows'].extend(result.get('upstream_cache_rows', []))
        
        # 处理每个 VRF 的数据
        for vrf, results in vrf_results.items():
            rows = results['rows']
            upstream_cache_rows = results['upstream_cache_rows']
            
            # 写入上游缓存
            if vrf == cache_vrf and upstream_cache_rows:
                storage.bulk_upsert_bgp_upstream_route_cache(conn, upstream_cache_rows)
            
            # 检查是否需要替换
            do_replace = True
            if not rows and vrf == cache_vrf:
                up_ip = bgp_sticky_reconcile.first_upstream_neighbor_ip(conn, vrf)
                if up_ip and not frr_bgp.neighbor_is_established(vrf, up_ip, sd):
                    do_replace = False
                    logger.info(
                        "bgp learn vrf=%s rib empty, upstream %s not Established — keeping SQLite snapshot",
                        vrf,
                        up_ip,
                    )
                elif not up_ip and storage.list_bgp_upstream_cache_rows(conn, vrf):
                    do_replace = False
                    logger.info(
                        "bgp learn vrf=%s rib empty, no upstream meta/env but upstream_route_cache non-empty — keeping SQLite snapshot",
                        vrf,
                    )
            
            if do_replace:
                storage.replace_bgp_learned_routes_for_vrf(conn, vrf, rows)
                try:
                    cache = get_global_cache()
                    cache.update_routes(vrf, rows)
                except Exception:
                    logger.exception("Failed to update route cache")
    
    finally:
        conn.close()
    
    logger.info(f"Multi-thread sync completed: {len(threads)} threads, {len(result_dict)} neighbors processed")
    return sticky_summary


def _process_vrf_routes(
    conn,
    vrf: str,
    cache_vrf: str,
    rows: List[tuple],
    upstream_ips: set[str],
    ts: str,
    sd: Dict[str, set[str]],
):
    """处理单个 VRF 的路由数据（单线程模式辅助函数）"""
    if vrf == cache_vrf:
        upstream_cache_rows: List[tuple] = []
        for r in storage.list_bgp_learned_routes(conn, vrf):
            role_o = str(r["role"] or "").strip().lower()
            nh_o = str(r["nexthop"] or "").strip()
            peer_o = str(r["neighbor_ip"] or "").strip()
            if role_o == "upstream" or nh_o in upstream_ips or peer_o in upstream_ips:
                upstream_cache_rows.append(
                    (vrf, str(r["prefix"]), nh_o, peer_o, int(r["remote_as"] or 0), str(r["as_path"] or "")[:512], ts)
                )
        for tup in rows:
            if len(tup) < 7:
                continue
            role_t = str(tup[4] or "").strip().lower()
            nh, peer = str(tup[1] or "").strip(), str(tup[2] or "").strip()
            if role_t == "upstream" or nh in upstream_ips or peer in upstream_ips:
                upstream_cache_rows.append((vrf, tup[0], nh, peer, int(tup[3]), tup[5], ts))
        if upstream_cache_rows:
            storage.bulk_upsert_bgp_upstream_route_cache(conn, upstream_cache_rows)
    
    do_replace = True
    if not rows:
        any_established = False
        for nip in sd.get(vrf, set()):
            if frr_bgp.neighbor_is_established(vrf, nip, sd):
                any_established = True
                break
        if not any_established:
            do_replace = False
            logger.info(
                "bgp learn vrf=%s rib empty, no neighbor Established — keeping SQLite snapshot",
                vrf,
            )

    if do_replace:
        storage.replace_bgp_learned_routes_for_vrf(conn, vrf, rows)
        try:
            cache = get_global_cache()
            cache.update_routes(vrf, rows)
        except Exception:
            logger.exception("Failed to update route cache")

