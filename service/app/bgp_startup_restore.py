"""部署 / Agent 重启后：等待 bgp-agent 就绪，从 SQLite meta 恢复 RR、下游邻居与网络前提。"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

import httpx

from . import bgp_control, bgp_ipvlan_reconcile, storage

logger = logging.getLogger(__name__)


def _agent_restore_max_wait_sec() -> int:
    try:
        return max(30, int(os.environ.get("MTR_BGP_AGENT_RESTORE_MAX_SEC", "600")))
    except ValueError:
        return 600


def wait_agent_healthy(
    max_sec: Optional[int] = None,
    interval: float = 5.0,
) -> bool:
    """轮询 Agent /health，大 RIB 恢复时可能需数分钟。"""
    deadline = time.monotonic() + (max_sec if max_sec is not None else _agent_restore_max_wait_sec())
    url = f"{bgp_control.agent_url()}/health"
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.get(url)
                if r.status_code == 200 and (r.json() or {}).get("status") == "ok":
                    return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _unfreeze_agent() -> None:
    base = bgp_control.agent_url()
    with httpx.Client(timeout=30.0) as c:
        for path in ("/api/rr/unfreeze",):
            try:
                c.post(f"{base}{path}")
            except Exception as e:
                logger.debug("agent unfreeze %s: %s", path, e)


def _configure_rr_from_meta(conn: sqlite3.Connection) -> List[str]:
    """按 meta 中 RR 行重新 POST /api/rr/config。"""
    done: List[str] = []
    seen_rr = set()
    for vrf, nip, role, _note, src in bgp_control._iter_meta(conn):
        if not bgp_control.is_rr_role(role):
            continue
        if nip in seen_rr:
            continue
        seen_rr.add(nip)
        la = (src or "").strip() or bgp_control.default_router_id()
        ras = bgp_control.default_local_as()
        try:
            for row in bgp_control.list_agent_neighbors():
                if str(row.get("address")) == nip:
                    ras = int(row.get("remote_as") or ras)
                    break
        except Exception:
            pass
        try:
            bgp_control.configure_rr(nip, ras, local_address=la)
            done.append(f"rr:{nip}")
        except Exception as e:
            logger.warning("configure_rr %s failed: %s", nip, e)
    if not done:
        env = bgp_control._agent_env()
        rr = (env.get("rr_addr") or os.environ.get("RR_ADDR") or "").strip()
        if rr:
            try:
                nip = storage.validate_ipv4(rr)
                bgp_control.configure_rr(
                    nip,
                    int(env.get("rr_as") or bgp_control.default_local_as()),
                    local_address=bgp_control.default_router_id(),
                )
                done.append(f"rr:{nip}:env")
            except Exception as e:
                logger.warning("configure_rr from env failed: %s", e)
    return done


def _reconcile_ipvlan_peers(conn: sqlite3.Connection) -> List[str]:
    """卫星 VRF 下游：ipvlan + DNAT，避免冒充源会话起不来。"""
    if not bgp_ipvlan_reconcile.enabled():
        return []
    db_path = bgp_control._op_db_path()
    steps: List[str] = []
    for vrf, nip, role, _note, _src in bgp_control._iter_meta(conn):
        if not bgp_control.is_downstream_role(role):
            continue
        if not bgp_control._satellite_style_vrf_name(vrf):
            continue
        try:
            r = bgp_ipvlan_reconcile.reconcile_vrf_from_op_database(
                db_path, vrf, peer_ip=nip
            )
            steps.append(f"{vrf}:{nip}:{r.get('ok', r)}")
        except Exception as e:
            logger.warning("ipvlan reconcile %s/%s: %s", vrf, nip, e)
            steps.append(f"{vrf}:{nip}:error")
    return steps


def _rr_aggregate_spoof_ip(rr_neighbor: str) -> str:
    env = (os.environ.get("BGP_AGGREGATE_SPOOF_IP") or os.environ.get("RR_ADDR") or "").strip()
    if env:
        return storage.validate_ipv4(env)
    return storage.validate_ipv4(rr_neighbor)


def _resume_rr_aggregate_advertise(conn: sqlite3.Connection) -> List[str]:
    """
    meta 中已打开「路由通告」的 RR 行：向 Agent 提交 RR 聚合通告任务（不阻塞等待完成）。
    用于部署/重启后库已灌满但 pfx_adv 仍停留在旧快照的场景。
    """
    from . import bgp_peer_rib

    started: List[str] = []
    seen_rr: set[str] = set()
    for vrf, nip, role, _note, _src in bgp_control._iter_meta(conn):
        if not bgp_control.is_rr_role(role):
            continue
        if nip in seen_rr:
            continue
        meta = storage.get_bgp_neighbor_meta_map(conn, vrf).get(nip)
        if not meta or not int(meta[3] if len(meta) > 3 else 0):
            continue
        seen_rr.add(nip)
        spoof = _rr_aggregate_spoof_ip(nip)
        peers = storage.get_downstream_neighbors(conn, spoof)
        if not peers:
            logger.info("resume rr advertise %s: no downstream for source_ip=%s", nip, spoof)
            continue
        task_id = f"{storage.validate_vrf_name(vrf)}-{storage.validate_ipv4(nip)}-advertise"
        src_peers = [{"window": "downstream", "vrf": v, "neighbor_ip": n} for v, n in peers]
        try:
            bgp_peer_rib.start_rib_advertise_job(
                task_id, "", "", "", target="rr", enable=True, src_peers=src_peers
            )
            started.append(f"{vrf}:{nip}:peers={len(peers)}")
            logger.info(
                "resume rr aggregate advertise task_id=%s spoof=%s peers=%d",
                task_id,
                spoof,
                len(peers),
            )
        except Exception as e:
            logger.warning("resume rr advertise %s failed: %s", nip, e)
    return started


def restore_from_sqlite(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    从 SQLite 恢复 Agent 侧 BGP（幂等，可重复调用）。
    部署脚本与 OP 启动后台任务均应调用此函数。
    """
    summary: Dict[str, Any] = {"ok": False}
    if not wait_agent_healthy():
        summary["error"] = "agent_not_healthy"
        return summary

    if bgp_ipvlan_reconcile.enabled():
        try:
            summary["lab_stack"] = bgp_ipvlan_reconcile.ensure_lab_network_stack(
                bgp_control._op_db_path()
            )
        except Exception as e:
            logger.warning("ensure_lab_network_stack: %s", e)
            summary["lab_stack_error"] = str(e)[:200]

    _unfreeze_agent()
    presets = storage.apply_bgp_db_presets(conn)
    conn.commit()
    summary["presets_applied"] = presets

    summary["rr_configured"] = _configure_rr_from_meta(conn)
    rec = bgp_control.reconcile_meta_to_agent(conn)
    summary["agent_reconcile"] = rec
    summary["ipvlan"] = _reconcile_ipvlan_peers(conn)
    if bgp_ipvlan_reconcile.satellite_dnat_enabled():
        try:
            summary["satellite_dnat"] = bgp_ipvlan_reconcile.reconcile_satellite_dnat(
                bgp_control._op_db_path()
            )
        except Exception as e:
            logger.warning("satellite_dnat reconcile: %s", e)

    # OP 层解冻（转发到 Agent）
    try:
        with httpx.Client(timeout=30.0) as c:
            r = c.post("http://127.0.0.1:8808/api/gobgp/unfreeze")
            summary["op_unfreeze"] = r.json() if r.content else {}
    except Exception as e:
        summary["op_unfreeze_error"] = str(e)[:200]

    summary["ok"] = not rec.get("skipped") and not rec.get("errors")
    if rec.get("errors"):
        summary["ok"] = len(rec.get("added") or []) > 0

    if os.environ.get("MTR_BGP_RESUME_ADVERTISE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    ):
        try:
            summary["resume_advertise"] = _resume_rr_aggregate_advertise(conn)
        except Exception as e:
            logger.warning("resume rr advertise: %s", e)
            summary["resume_advertise_error"] = str(e)[:200]

    logger.info("bgp restore_from_sqlite: %s", summary)
    return summary
