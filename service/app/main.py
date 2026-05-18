"""MTR/ICMP 运维 OP — FastAPI。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from . import (
    arp_spoof_assign,
    bgp_ipvlan_reconcile,
    bgp_learned_routes_sync,
    bgp_sticky_reconcile,
    gobgp_client,
    bgp_control,
    kernel_vrf,
    nft_sync,
    satellite_vrf_assign,
    storage,
    te_rewrite_sync,
    vpn_egress,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("MTR_OP_DB", str(ROOT / "data.db")))
NFT_FILE = Path(os.environ.get("MTR_OP_NFT", str(ROOT / "nft_mtr_spoof.nft")))
DATA_DIR = Path(os.environ.get("MTR_OP_DATA", str(ROOT / "data")))

_BG_RIB_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="op_bgp_rib")

_ADVERTISE_TASKS: Dict[str, Dict[str, Any]] = {}
_ADVERTISE_LOCK = asyncio.Lock()


async def _run_blocking_call(func, /, *args, **kwargs):
    """Python 3.8 无 asyncio.to_thread，用线程池执行阻塞 I/O（Agent / SQLite）。"""
    loop = asyncio.get_event_loop()
    if kwargs:
        return await loop.run_in_executor(_BG_RIB_EXECUTOR, functools.partial(func, *args, **kwargs))
    if not args:
        return await loop.run_in_executor(_BG_RIB_EXECUTOR, func)
    return await loop.run_in_executor(_BG_RIB_EXECUTOR, functools.partial(func, *args))


def _db() -> sqlite3.Connection:
    conn = storage.connect(DB_PATH)
    storage.init_schema(conn)
    storage.seed_defaults(conn)
    return conn


def _sync_nft_from_conn(conn: sqlite3.Connection) -> None:
    g = storage.get_global(conn)
    nft_sync.sync_nft(
        nft_file=NFT_FILE,
        hijack_enabled=g.hijack_enabled,
        hop_rules=storage.list_hop_rules_enabled(conn),
    )


def _apply_nft(conn: sqlite3.Connection, *, hijack_enabled: bool | None = None) -> None:
    if os.environ.get("MTR_OP_SKIP_NFT_SYNC", "").strip().lower() in {"1", "true", "yes"}:
        logger.warning("MTR_OP_SKIP_NFT_SYNC is set: skipping nft sync after global/hop changes")
        return
    try:
        enabled = hijack_enabled if hijack_enabled is not None else storage.get_global(conn).hijack_enabled
        nft_sync.sync_nft(
            nft_file=NFT_FILE,
            hijack_enabled=enabled,
            hop_rules=storage.list_hop_rules_enabled(conn),
        )
    except Exception as e:
        logger.exception("nft sync failed")
        raise HTTPException(status_code=500, detail=f"nft_sync_failed: {e}") from e


def _sync_te_rewrite_best_effort(conn: sqlite3.Connection) -> None:
    try:
        te_rewrite_sync.sync_te_rewrite_from_conn(conn)
    except Exception:
        logger.exception("te_rewrite sync failed")


async def _bgp_rib_sync_loop() -> None:
    from . import bgp_bidirectional_sync

    await asyncio.sleep(8)
    interval = int(os.environ.get("MTR_BGP_RIB_SYNC_SEC", "60"))
    while True:
        try:
            await _run_blocking_call(bgp_bidirectional_sync.sync_bidirectional_routes, DB_PATH)
        except Exception:
            logger.exception("bgp bidirectional periodic sync")
        await asyncio.sleep(max(30, interval))


async def _vpn_reconcile_loop() -> None:
    await asyncio.sleep(20)
    interval = int(os.environ.get("MTR_VPN_RECONCILE_SEC", "90"))
    while True:
        try:
            conn = _db()
            try:
                vpn_egress.reconcile_status(conn)
            finally:
                conn.close()
        except Exception:
            logger.exception("vpn reconcile loop")
        await asyncio.sleep(max(30, interval))


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 初始化 GoBGP Agent 客户端
    gobgp = gobgp_client.get_gobgp_client()
    logger.info("GoBGP Agent客户端已初始化")
    
    # 检查GoBGP Agent健康状态
    try:
        health = await gobgp.health()
        logger.info(f"GoBGP Agent状态: {health.get('status')}")
    except Exception:
        logger.warning("GoBGP Agent未运行，部分功能可能不可用")

    # 初始化 BGP 路由缓存
    try:
        from .bgp_route_cache import get_global_cache
        cache = get_global_cache()
        logger.info("BGP route cache initialized")
    except Exception:
        logger.exception("Failed to initialize BGP route cache")

    conn = _db()
    try:
        try:
            g = storage.get_global(conn)
            _sync_nft_from_conn(conn)
        except Exception:
            logger.exception("startup nft sync failed (fix CAP_NET_ADMIN / nft path)")
        try:
            te_rewrite_sync.sync_te_rewrite_from_conn(conn)
        except Exception:
            logger.exception("startup te_rewrite sync failed")
        try:
            _arp_reconcile_host_ip_best_effort()
        except Exception:
            logger.exception("startup arp host-ip reconcile failed")
        try:
            _seed_bgp_neighbors_from_frr(conn)
        except Exception:
            logger.exception("startup bgp meta seed from frr failed")
        try:
            r = bgp_control.reconcile_meta_to_agent(conn)
            logger.info("gobgp startup reconcile: %s", r)
        except Exception:
            logger.exception("gobgp startup reconcile failed")
    finally:
        conn.close()
    rib_task = None
    if os.environ.get("MTR_BGP_RIB_SYNC", "1").strip().lower() not in {"0", "false", "no"}:
        rib_task = asyncio.create_task(_bgp_rib_sync_loop())
    vpn_task = None
    if os.environ.get("MTR_VPN_RECONCILE", "1").strip().lower() not in {"0", "false", "no"}:
        vpn_task = asyncio.create_task(_vpn_reconcile_loop())
    try:
        yield
    finally:
        # 关闭GoBGP客户端
        await gobgp_client.close_gobgp_client()
        if vpn_task:
            vpn_task.cancel()
            try:
                await vpn_task
            except asyncio.CancelledError:
                pass
        if rib_task:
            rib_task.cancel()
            try:
                await rib_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="MTR ICMP OP", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = ROOT / "static"


def _signal_arp_daemon_reload() -> None:
    """供独立 ARP 守护可选监听（轮询 DB 时可忽略）。"""
    p = Path(os.environ.get("MTR_ARP_RELOAD_FILE", str(DATA_DIR / ".arp_reload")))
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        logger.warning("write MTR_ARP_RELOAD_FILE failed", exc_info=True)


def _arp_reconcile_host_ip_best_effort() -> None:
    """
    按 OP 库中 ARP 引流配置，在本机接口上增删冒充网关的 /32（与 arp_spoof_daemon 一致）。

    需 CAP_NET_ADMIN；OP 通常以 root 跑。可用 ``MTR_OP_ARP_ASSIGN_HOST_IP=0`` 关闭。
    """
    if os.environ.get("MTR_OP_ARP_ASSIGN_HOST_IP", "1").strip().lower() in {"0", "false", "no"}:
        return
    try:
        r = arp_spoof_assign.reconcile_from_op_database(DB_PATH)
        logger.info("arp_spoof_assign: %s", r)
    except Exception:
        logger.exception("arp_spoof_assign reconcile failed")


def _satellite_vrf_reconcile_best_effort() -> None:
    """ARP 库变更后：可选按环境变量自动建卫星 VRF（``satellite_vrf_assign``）。"""
    if bgp_ipvlan_reconcile.enabled():
        logger.info("skip legacy satellite_vrf_assign because MTR_BGP_IPVLAN_AUTO is enabled")
        return
    try:
        satellite_vrf_assign.reconcile_best_effort(DB_PATH)
    except Exception:
        logger.exception("satellite_vrf_assign reconcile failed")


def _bgp_ipvlan_reconcile_best_effort() -> None:
    """按 OP 库中的 satellite_vrf 条目，收敛 ipvlan L2 + VRF BGP 源地址。"""
    try:
        r = bgp_ipvlan_reconcile.reconcile_from_op_database(DB_PATH)
        logger.info("bgp_ipvlan_reconcile: %s", r)
    except Exception:
        logger.exception("bgp_ipvlan_reconcile failed")


def _bgp_ipvlan_reconcile_vrf_best_effort(vrf_norm: str, peer_ip: Optional[str] = None) -> None:
    """新增/修改 BGP 邻居后，按库中邻居 IP 刷新该卫星 VRF 的 ipvlan 与 VRF 路由。"""
    try:
        r = bgp_ipvlan_reconcile.reconcile_vrf_from_op_database(DB_PATH, vrf_norm, peer_ip=peer_ip)
        logger.info("bgp_ipvlan_reconcile vrf=%s: %s", vrf_norm, r)
    except Exception:
        logger.exception("bgp_ipvlan_reconcile failed vrf=%s", vrf_norm)


def _bgp_ipvlan_reconcile_vrf_required(vrf_norm: str, peer_ip: Optional[str] = None) -> None:
    """BGP 新增邻居时：用用户填写的对端 IP 写 VRF 路由并校验 ipvlan。"""
    try:
        r = bgp_ipvlan_reconcile.reconcile_vrf_from_op_database(DB_PATH, vrf_norm, peer_ip=peer_ip)
    except Exception as e:
        logger.exception("bgp_ipvlan_reconcile required failed vrf=%s", vrf_norm)
        raise HTTPException(status_code=503, detail=f"bgp_ipvlan_reconcile_failed: {e}") from e
    if r.get("skipped") or r.get("ok") is False:
        raise HTTPException(status_code=503, detail={"code": "bgp_ipvlan_reconcile_failed", "result": r})


def _arp_auto_satellite_vrf(spoof_ip: str, satellite_vrf: Optional[str], note: str) -> str:
    """ARP 未填 satellite_vrf 时：备注含 BGPSAT 或开启 ipvlan 则自动 ``vbgp{去点 IP}``。"""
    s = (satellite_vrf or "").strip()
    if s:
        return s
    from .vrf_naming import satellite_vrf_name

    if "BGPSAT" in (note or "").upper():
        return satellite_vrf_name(spoof_ip)
    if bgp_ipvlan_reconcile.enabled():
        raw = (os.environ.get("MTR_AUTO_FILL_SATELLITE_VRF") or "1").strip().lower()
        if raw not in {"", "0", "off", "false", "no"}:
            return satellite_vrf_name(spoof_ip)
    return ""


def _arp_delete_remove_bgp_enabled() -> bool:
    return os.environ.get("MTR_ARP_DELETE_REMOVE_BGP", "1").strip().lower() not in {"0", "false", "no"}


def _remove_bgp_neighbors_for_vrf(conn, vrf_norm: str) -> List[Dict[str, Any]]:
    """删除卫星 VRF 下全部 Agent 邻居与 OP 元数据（删 ARP 引流时对称清理）。"""
    removed: List[Dict[str, Any]] = []
    meta_map = storage.get_bgp_neighbor_meta_map(conn, vrf_norm)
    for nip in list(meta_map.keys()):
        meta = meta_map.get(nip)
        role = (meta[0] if meta else "unknown") or "unknown"
        try:
            if bgp_control.is_rr_role(role):
                bgp_control.remove_rr(nip)
            else:
                bgp_control.remove_neighbor(vrf_norm, nip)
        except Exception as e:
            logger.warning("remove bgp neighbor %s/%s: %s", vrf_norm, nip, e)
        storage.delete_bgp_neighbor_meta(conn, vrf_norm, nip)
        storage.delete_bgp_learned_routes_by_neighbor_ip(conn, nip)
        removed.append({"vrf": vrf_norm, "neighbor_ip": nip})
    return removed


def _arp_target_after_write_reconcile(conn, *, vrf_hint: str = "") -> None:
    """ARP 写库后：卫星 VRF/ipvlan、主机 /32、若已有 BGP 邻居则补 VRF 路由与 DNAT。"""
    if bgp_ipvlan_reconcile.enabled():
        _bgp_ipvlan_reconcile_best_effort()
    else:
        _satellite_vrf_reconcile_best_effort()
    _arp_reconcile_host_ip_best_effort()
    vrf = (vrf_hint or "").strip()
    if vrf and bgp_ipvlan_reconcile.enabled():
        peer = storage.downstream_neighbor_ip_for_vrf(conn, vrf)
        if peer:
            _bgp_ipvlan_reconcile_vrf_best_effort(vrf, peer_ip=peer)


def _satellite_vrf_prefix_str() -> str:
    p = (os.environ.get("MTR_SATELLITE_VRF_PREFIX") or "vbgp").strip()
    return p if p else "vbgp"


def _satellite_style_vrf_name(vrf: str) -> bool:
    p = _satellite_vrf_prefix_str()
    if not vrf.startswith(p):
        return False
    return vrf[len(p) :].isdigit()


def _satellite_bgp_tcp_source_mode() -> str:
    m = (os.environ.get("MTR_SATELLITE_BGP_TCP_SOURCE") or "underlay").strip().lower()
    if m not in {"underlay", "spoof"}:
        m = "underlay"
    if m == "spoof" and satellite_vrf_assign._phy_is_main(satellite_vrf_assign._phy_vrf()):
        logger.warning(
            "MTR_SATELLITE_BGP_TCP_SOURCE=spoof 且 MTR_SATELLITE_PHY_VRF 为主表/default："
            "若 Linux 201 仍以 10.133.152.25x 为 BGP 邻居地址，请在 200 上加载 nft "
            "inet nat_sat_bgp（仓库 scripts/ensure_nat_sat_bgp_linux200.sh），"
            "或改为 MTR_SATELLITE_BGP_TCP_SOURCE=underlay 且 201 neighbor 改为对应 veth 本端 10.255.x.1"
        )
    return m


def _resolve_satellite_bgp_source_ip(vrf_norm: str, body_source: Optional[str]) -> str:
    """OP 省略 source_ip 时，卫星 VRF 在 underlay 模式下使用 veth 本端 ``10.255.x.1`` 作 BGP TCP 源。"""
    sip0 = (body_source or "").strip()
    if sip0:
        return sip0
    if not _satellite_style_vrf_name(vrf_norm):
        return ""
    if bgp_ipvlan_reconcile.enabled():
        sip = bgp_ipvlan_reconcile.source_ip_for_vrf(DB_PATH, vrf_norm)
        return sip or ""
    if _satellite_bgp_tcp_source_mode() != "underlay":
        return ""
    u = satellite_vrf_assign.underlay_local_ip_for_vrf(vrf_norm, DB_PATH)
    return u or ""


def _satellite_bgp_ebgp_multihop(vrf_norm: str) -> Optional[int]:
    if _satellite_style_vrf_name(vrf_norm) and bgp_ipvlan_reconcile.enabled():
        return None
    return kernel_vrf.ebgp_multihop_satellite_default() if _satellite_style_vrf_name(vrf_norm) else None


def _bgp_auto_create_kernel_vrf_enabled() -> bool:
    return os.environ.get("MTR_BGP_AUTO_CREATE_KERNEL_VRF", "1").strip().lower() not in {"0", "false", "no"}


def _ensure_kernel_vrf_if_missing(vrf_norm: str, create: bool, rt_table: Optional[int]) -> None:
    """非 default：若请求且内核尚无 VRF 设备，则 ``ip link add … type vrf``。"""
    if vrf_norm == "default" or not create or not _bgp_auto_create_kernel_vrf_enabled():
        return
    if vrf_norm in set(kernel_vrf.list_kernel_vrf_names()):
        return
    try:
        kernel_vrf.ensure_kernel_vrf(vrf_norm, rt_table)
    except kernel_vrf.KernelVrfError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _list_net_ifaces_linux() -> List[str]:
    base = Path("/sys/class/net")
    if not base.is_dir():
        return []
    names: List[str] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name == "lo" or child.name.startswith("."):
            continue
        names.append(child.name)
    return names


class GlobalIn(BaseModel):
    hijack_enabled: bool


class GlobalOut(BaseModel):
    hijack_enabled: bool


class HopRuleIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    match_cidr: str
    forged_src: str
    priority: int = 0
    enabled: bool = True
    note: str = ""


class HopRulePatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    match_cidr: Optional[str] = None
    forged_src: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    note: Optional[str] = None


class HopRuleOut(BaseModel):
    id: int
    match_cidr: str
    forged_src: str
    priority: int
    enabled: bool
    note: str
    created_at: str


def _hop_rule_out(row) -> HopRuleOut:
    """接受 ``HopReplaceRule`` 或 dict。"""
    if hasattr(row, "id"):
        return HopRuleOut(
            id=int(row.id),
            match_cidr=str(row.match_cidr),
            forged_src=str(row.forged_src),
            priority=int(row.priority),
            enabled=bool(row.enabled),
            note=str(row.note or ""),
            created_at=str(row.created_at),
        )
    return HopRuleOut(
        id=int(row["id"]),
        match_cidr=str(row["match_cidr"]),
        forged_src=str(row["forged_src"]),
        priority=int(row["priority"]),
        enabled=bool(row["enabled"]),
        note=str(row["note"] or ""),
        created_at=str(row["created_at"]),
    )


class ArpSpoofSettingsIn(BaseModel):
    arp_spoof_enabled: bool = False


class ArpSpoofSettingsOut(BaseModel):
    arp_spoof_enabled: bool


class ArpTargetIn(BaseModel):
    spoof_gateway_ip: str
    satellite_vrf: Optional[str] = None
    egress_iface: str
    enabled: bool = True
    policy_mode: str = "gateway_only"
    policy_cidrs: str = ""
    note: str = ""


class ArpTargetPatch(BaseModel):
    spoof_gateway_ip: Optional[str] = None
    satellite_vrf: Optional[str] = None
    egress_iface: Optional[str] = None
    enabled: Optional[bool] = None
    policy_mode: Optional[str] = None
    policy_cidrs: Optional[str] = None
    note: Optional[str] = None


class ArpTargetOut(BaseModel):
    id: int
    enabled: bool
    spoof_gateway_ip: str
    satellite_vrf: Optional[str] = None
    egress_iface: str
    policy_mode: str
    policy_cidrs: str
    note: str
    created_at: str


class BgpVrfOut(BaseModel):
    vrf: str
    local_as: int
    has_router_bgp: bool = Field(
        default=True,
        description="若为 False：内核已有 VRF 设备但尚未通过 OP/Agent 使用该 VRF，可先 POST /api/bgp/instances 创建内核 VRF",
    )


class BgpEnsureInstanceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    vrf: str
    local_as: Optional[int] = Field(
        default=None,
        description="省略则等同 ``MTR_BGP_ENSURE_LOCAL_AS`` 或非 default 实例的 AS",
    )
    router_id: Optional[str] = Field(default=None, description="可选 ``bgp router-id``")
    create_kernel_vrf_if_missing: bool = Field(
        default=True,
        description="若内核尚无该 VRF 设备，是否先 ``ip link add … type vrf`` 再建仓",
    )
    kernel_rt_table: Optional[int] = Field(
        default=None,
        ge=1,
        le=999999,
        description="创建 Linux VRF 时的 ``rt_table``；省略则自动挑选空闲表号",
    )


class BgpNeighborIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    vrf: str = "default"
    neighbor_ip: str
    remote_as: int
    role: str = "auto"
    source_ip: Optional[str] = Field(
        default=None,
        description=(
            "本端 TCP/BGP 源地址（GoBGP TX ``local_address`` / update-source）；通常与 ARP 引流「冒充网关 IPv4」一致。"
            "卫星 VRF（名前缀同 ``MTR_SATELLITE_VRF_PREFIX``，如 vbgp10133152250）且 ``MTR_SATELLITE_BGP_TCP_SOURCE=underlay``（默认）时，"
            "省略本字段则自动使用卫星 veth 本端 ``10.255.x.1``，以便与 Linux 201 建连；显式填写则沿用该地址。"
        ),
    )
    bgp_local_as: Optional[int] = Field(
        default=None,
        ge=1,
        le=4294967295,
        description="当所选 VRF 尚无 GoBGP TX 实例时，用该 AS 创建内核 VRF（Agent 按 VRF 懒启动）；省略则用环境默认 AS",
    )
    bgp_router_id: Optional[str] = Field(
        default=None,
        description="自动创建 BGP 实例时写入 ``bgp router-id``；也可设环境变量 ``MTR_BGP_ENSURE_ROUTER_ID``",
    )
    create_kernel_vrf_if_missing: bool = Field(
        default=True,
        description="若内核尚无该 VRF 设备，是否先 ``ip link add … type vrf``（再 ``router bgp`` / ``neighbor``）",
    )
    kernel_rt_table: Optional[int] = Field(
        default=None,
        ge=1,
        le=999999,
        description="创建 Linux VRF 时的 ``rt_table``；省略则自动挑选空闲表号",
    )
    satellite_vrf: Optional[str] = Field(
        default=None,
        description="所在VRF名称，数据来源satellite_vrf；选择后自动创建空VRF",
    )


class BgpNeighborPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    neighbor_ip: Optional[str] = Field(
        default=None,
        description="若与 URL 中邻居 IP 不同，则经 Agent 删旧邻、以新 IP 重建；未传的其它字段沿用当前值",
    )
    remote_as: Optional[int] = None
    role: Optional[str] = None
    note: Optional[str] = None
    source_ip: Optional[str] = Field(
        default=None,
        description="置空字符串表示清除 update-source；省略字段表示不改",
    )


class BgpNeighborToggleIn(BaseModel):
    enabled: bool


class BgpNeighborAdvertiseIn(BaseModel):
    advertise_routes: int = Field(default=0, ge=0, le=1, description="1=向对端通告本窗持久库路由，0=撤销")


class BgpNeighborStoreIn(BaseModel):
    store_received_routes: int = Field(
        default=0, ge=0, le=1, description="1=将从对端收到的路由写入 Agent Redis/RocksDB"
    )


class BgpNeighborOut(BaseModel):
    """BGP 管理列表与操作 API 使用的邻居摘要。"""

    vrf: str
    neighbor_ip: str
    local_as: int
    remote_as: int
    source_ip: str = ""
    role: str
    session_state: str
    routes_received: int = Field(default=0, description="从对端收到的路由前缀数（BGP AFI/SAFI Received）")
    routes_sent: int = Field(default=0, description="向对端通告的路由前缀数（BGP AFI/SAFI Advertised）")
    enabled: bool = True
    advertise_routes: int = Field(default=0, description="1=向对端通告本窗持久库路由（Agent Redis/RocksDB）")
    store_received_routes: int = Field(default=0, description="1=持久化从对端收到的路由到 Agent")
    routes_cached: int = Field(default=0, description="Agent 持久库中该 peer 路由条数")
    note: str = Field(default="", description="运维备注（仅编辑弹窗使用，列表不展示）")


class BgpLearnedRouteOut(BaseModel):
    vrf: str
    prefix: str
    nexthop: str
    neighbor_ip: str
    remote_as: int
    role: str
    as_path: str
    updated_at: str
    route_window: str = "upstream"
    peer_frozen: bool = False
    persisted: bool = True
    stale: bool = False
    data_source: str = Field(
        default="rib_agent",
        description="rib_agent=Agent Redis/RocksDB；rib_sqlite=旧 SQLite 快照（兼容）",
    )


class BgpLearnedRoutesSnapshotOut(BaseModel):
    last_sync_at: Optional[str] = None
    last_sync_ok: bool = True
    last_sync_error: str = ""
    routes: List[BgpLearnedRouteOut]
    total: int = 0
    page: int = 1
    page_size: int = 100
    route_window: Optional[str] = None
    summary: Dict[str, int] = Field(default_factory=dict)
    peer_snapshots: List[Dict[str, Any]] = Field(default_factory=list)


# ----- VPN egress API -----


class VpnLinkIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    link_type: str
    vrf: str = "vrf2103"
    endpoint: str = ""
    iface_name: str = ""
    enabled: bool = True
    desired_up: bool = True
    priority: int = 100
    config: Dict[str, Any] = {}


class VpnLinkPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    link_type: Optional[str] = None
    vrf: Optional[str] = None
    endpoint: Optional[str] = None
    iface_name: Optional[str] = None
    enabled: Optional[bool] = None
    desired_up: Optional[bool] = None
    priority: Optional[int] = None
    config: Optional[Dict[str, Any]] = None


class VpnLinkOut(BaseModel):
    id: int
    name: str
    link_type: str
    vrf: str
    endpoint: str
    iface_name: str
    enabled: bool
    desired_up: bool
    priority: int
    config: Dict[str, Any]
    last_error: str
    last_rtt_ms: Optional[float] = None
    actual_status: str
    rx_bytes: int
    tx_bytes: int
    stats_updated_at: Optional[str] = None
    created_at: str
    updated_at: str


class VpnPolicyIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dst_cidr: str
    src_cidr: str = ""
    src_label: str = ""
    vpn_link_id: int
    backup_link_id: Optional[int] = None
    fail_action: str = "fallback"
    enabled: bool = True


class VpnPolicyPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dst_cidr: Optional[str] = None
    src_cidr: Optional[str] = None
    src_label: Optional[str] = None
    vpn_link_id: Optional[int] = None
    backup_link_id: Optional[int] = None
    fail_action: Optional[str] = None
    enabled: Optional[bool] = None


class VpnPolicyOut(BaseModel):
    id: int
    dst_cidr: str
    src_cidr: str
    src_label: str
    vpn_link_id: int
    backup_link_id: Optional[int] = None
    fail_action: str
    enabled: bool
    created_at: str
    updated_at: str


class VpnPingIn(BaseModel):
    target: str
    vrf: str = "vrf2103"
    count: int = 3


class VpnSummaryOut(BaseModel):
    total: int
    up: int
    down: int
    disabled: int


def _vpn_link_out(row: dict) -> VpnLinkOut:
    return VpnLinkOut(**row)


def _vpn_policy_out(row: dict) -> VpnPolicyOut:
    return VpnPolicyOut(**row)


def _seed_bgp_neighbors_from_frr(conn: sqlite3.Connection) -> List[str]:
    """将 GoBGP Agent 已有邻居写入 SQLite，再套用预设角色。"""
    try:
        for row in bgp_control.list_agent_neighbors():
            vrf = storage.validate_vrf_name(str(row.get("vrf") or "default"))
            ip = storage.validate_ipv4(str(row.get("address") or ""))
            role = str(row.get("role") or "unknown")
            src = str(row.get("local_address") or "")
            storage.ensure_bgp_neighbor_meta_row(conn, vrf, ip)
            if role != "unknown":
                storage.set_bgp_neighbor_meta(
                    conn,
                    vrf,
                    ip,
                    role,
                    "",
                    update_source=src if storage.is_usable_bgp_source_ip(src) else None,
                )
    except Exception:
        logger.exception("seed neighbors from gobgp agent")
    return storage.apply_bgp_db_presets(conn)


def _bgp_role_hints() -> dict:
    return storage.default_bgp_role_hints()


def _resolve_bgp_role(conn: sqlite3.Connection, vrf: str, neighbor_ip: str) -> tuple[str, str]:
    """
    返回 (role, role_source)。
    role_source: manual（库中非 unknown）| hint（默认映射）| unset
    """
    meta = storage.get_bgp_neighbor_meta_map(conn, vrf).get(neighbor_ip)
    db_role = (meta[0] if meta else None) or "unknown"
    if db_role != "unknown":
        return db_role, "manual"
    h = _bgp_role_hints().get(neighbor_ip)
    if h:
        return h, "hint"
    return "unknown", "unset"


def _resolve_neighbor_source_ip(agent_row: Dict[str, Any], meta: Optional[tuple]) -> str:
    """展示用 TCP 源：Agent 在邻居 shutdown 时常返回 0.0.0.0，回退 SQLite meta。"""
    agent_src = str(agent_row.get("local_address") or "").strip()
    meta_src = str(meta[2] or "").strip() if meta and len(meta) > 2 else ""
    if storage.is_usable_bgp_source_ip(agent_src):
        return agent_src
    if storage.is_usable_bgp_source_ip(meta_src):
        return meta_src
    return meta_src or agent_src


def _collect_learned_route_peers(
    conn: sqlite3.Connection,
    q_vrf: Optional[str] = None,
    neighbor_ip: Optional[str] = None,
    route_window: Optional[str] = None,
) -> list[tuple[str, str, str]]:
    """可查询的 (vrf, neighbor_ip, role)。

    VRF 与邻居 IP 为**或**关系：只填其一则按该项筛选；两项都填则命中 VRF 或 邻居任一即纳入；
    均为空（全部）时不做这两项限制。route_window 仍与二者为且关系。
    """
    rw_raw = (route_window or "").strip().lower() or None
    if q_vrf:
        q_vrf = storage.validate_vrf_name(q_vrf)
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str]] = []

    def _matches_vrf_or_neighbor(v: str, ip: str) -> bool:
        if not q_vrf and not neighbor_ip:
            return True
        if q_vrf and v == q_vrf:
            return True
        if neighbor_ip and ip == neighbor_ip:
            return True
        return False

    def _add(v: str, ip: str, role: str) -> None:
        if not ip:
            return
        if not _matches_vrf_or_neighbor(v, ip):
            return
        rw = storage.route_window_for_bgp_role(role)
        if rw_raw and rw != rw_raw:
            return
        pair = (v, ip)
        if pair in seen:
            return
        seen.add(pair)
        out.append((v, ip, role))

    for row in conn.execute(
        "SELECT vrf, neighbor_ip, role FROM bgp_neighbor_meta ORDER BY vrf, neighbor_ip"
    ):
        v = storage.validate_vrf_name(str(row[0] or "default"))
        try:
            ip = storage.validate_ipv4(str(row[1] or ""))
        except ValueError:
            continue
        role, _ = _resolve_bgp_role(conn, v, ip)
        _add(v, ip, role)

    rx = bgp_control.get_rr_rx_neighbor_row()
    if rx:
        v = bgp_control.GOBGP_VRF_RR
        ip = str(rx.get("address") or "").strip()
        if ip:
            _add(v, ip, "rr")

    for row in bgp_control.list_agent_neighbors():
        v = storage.validate_vrf_name(str(row.get("vrf") or "default"))
        if v == bgp_control.GOBGP_VRF_RR and str(row.get("session") or "").lower() != "rx":
            continue
        ip = str(row.get("address") or "").strip()
        if not ip:
            continue
        role, _ = _resolve_bgp_role(conn, v, ip)
        _add(v, ip, role)

    out.sort()
    return out


def _iter_rr_meta_rows(conn: sqlite3.Connection):
    for row in conn.execute(
        "SELECT vrf, neighbor_ip, source_ip FROM bgp_neighbor_meta WHERE role = 'rr'"
    ):
        yield (
            storage.validate_vrf_name(str(row["vrf"])),
            storage.validate_ipv4(str(row["neighbor_ip"])),
            {"source_ip": str(row["source_ip"] or "")},
        )


def _agent_neighbor_row(
    conn: sqlite3.Connection, vrf_norm: str, neighbor_ip: str
) -> Optional[Dict[str, Any]]:
    """同一对端 IP 可存在于 default 与卫星 VRF，须按 vrf+ip 匹配 Agent 行。"""
    nip = str(neighbor_ip).strip()
    role, _ = _resolve_bgp_role(conn, vrf_norm, nip)
    if bgp_control.is_rr_role(role) or vrf_norm == bgp_control.GOBGP_VRF_RR:
        for rx in bgp_control.list_rr_rx_neighbor_rows():
            if str(rx.get("address") or "").strip() == nip:
                return rx
        return None
    for row in bgp_control.list_agent_neighbors():
        if str(row.get("address") or "").strip() != nip:
            continue
        if str(row.get("session") or "").lower() == "rx":
            continue
        if storage.validate_vrf_name(str(row.get("vrf") or "default")) == vrf_norm:
            return row
    return None


def _resolve_routes_sent(
    conn: sqlite3.Connection,
    vrf: str,
    ip: str,
    role: str,
    row: Optional[Dict[str, Any]] = None,
) -> int:
    """发送路由数：优先 BGP 会话 Advertised；开通告且会话为 0 时用任务进度/来源持久库条数。"""
    row = row or {}
    meta = storage.get_bgp_neighbor_meta_map(conn, vrf).get(ip)
    pfx_adv = int(row.get("pfx_adv") or 0)
    ar = int(meta[3]) if meta and len(meta) > 3 else 0
    if pfx_adv > 0:
        return pfx_adv
    if not ar:
        return 0
    task_id = f"{storage.validate_vrf_name(vrf)}-{storage.validate_ipv4(ip)}-advertise"
    task = _ADVERTISE_TASKS.get(task_id)
    if task:
        st = str(task.get("status") or "")
        added = int(task.get("added") or 0)
        total = int(task.get("total_routes") or 0)
        if st == "completed" and added > 0:
            return added
        if st == "running" and (added > 0 or total > 0):
            return added if added > 0 else total
    try:
        from . import bgp_peer_rib

        src_v, src_n, src_r = _advertise_rib_source(conn, vrf, ip, role)
        return bgp_peer_rib.count_peer_rib_routes(src_v, src_n, src_r)
    except Exception:
        return 0


def _neighbor_out_from_agent(conn: sqlite3.Connection, row: Dict[str, Any]) -> BgpNeighborOut:
    vrf = storage.validate_vrf_name(str(row.get("vrf") or "default"))
    ip = storage.validate_ipv4(str(row.get("address") or ""))
    role, _rs = _resolve_bgp_role(conn, vrf, ip)
    meta = storage.get_bgp_neighbor_meta_map(conn, vrf).get(ip)
    note = (meta[1] if meta else "") or ""
    src = _resolve_neighbor_source_ip(row, meta)
    ar = int(meta[3]) if meta and len(meta) > 3 else 0
    sr = storage.get_bgp_neighbor_store_received_routes(conn, vrf, ip)
    cached = 0
    try:
        from . import bgp_peer_rib

        cached = bgp_peer_rib.count_peer_rib_routes(vrf, ip, role)
    except Exception:
        cached = 0
    return BgpNeighborOut(
        vrf=vrf,
        neighbor_ip=ip,
        remote_as=int(row.get("remote_as") or bgp_control.default_local_as()),
        role=role,
        note=note,
        source_ip=src,
        local_as=int(bgp_control.default_local_as()),
        enabled=bool(row.get("enabled", True)),
        session_state=bgp_control.agent_row_to_state_label(str(row.get("state") or "")),
        routes_received=int(row.get("pfx_rcd") or 0),
        routes_sent=_resolve_routes_sent(conn, vrf, ip, role, row),
        advertise_routes=ar,
        store_received_routes=sr,
        routes_cached=cached,
    )


def _bgp_add_neighbor_impl(conn: sqlite3.Connection, body: BgpNeighborIn) -> BgpNeighborOut:
    vrf_norm = storage.validate_vrf_name(body.vrf)
    ip = storage.validate_ipv4(body.neighbor_ip)
    if int(body.remote_as) <= 0 or int(body.remote_as) > 4294967295:
        raise HTTPException(status_code=400, detail="invalid_remote_as")
    role_in = (body.role or "auto").strip().lower()
    if role_in == "auto":
        role = _bgp_role_hints().get(ip, "unknown")
    elif role_in in storage.BGP_META_ROLES:
        role = role_in
    else:
        role = "unknown"
    if bgp_control.is_rr_role(role):
        vrf_use = vrf_norm if vrf_norm not in ("", "default") else bgp_control.GOBGP_VRF_RR
        sip = (body.source_ip or "").strip() or (body.bgp_router_id or "").strip() or bgp_control.default_router_id()
        if ip in storage.get_bgp_neighbor_meta_map(conn, vrf_use):
            raise HTTPException(status_code=409, detail={"code": "neighbor_already_exists", "vrf": vrf_use, "neighbor_ip": ip})
        try:
            bgp_control.configure_rr(ip, int(body.remote_as), local_address=sip)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"gobgp_rr_failed: {e}") from e
        storage.set_bgp_neighbor_meta(conn, vrf_use, ip, role, "", update_source=sip)
        storage.update_bgp_neighbor_advertise_routes(conn, vrf_use, ip, 0, "")
        for rx in bgp_control.list_rr_rx_neighbor_rows():
            if str(rx.get("address") or "").strip() == ip:
                return _neighbor_out_from_agent(conn, rx)
        return _neighbor_out_from_agent(
            conn,
            {
                "vrf": vrf_use,
                "address": ip,
                "remote_as": body.remote_as,
                "state": "IDLE",
                "enabled": True,
                "local_address": sip,
            },
        )
    sip = _resolve_satellite_bgp_source_ip(vrf_norm, body.source_ip)
    mh = _satellite_bgp_ebgp_multihop(vrf_norm)
    bind_if = ""
    passive = False
    _ensure_kernel_vrf_if_missing(vrf_norm, body.create_kernel_vrf_if_missing, body.kernel_rt_table)
    if _agent_neighbor_row(conn, vrf_norm, ip):
        raise HTTPException(status_code=409, detail={"code": "neighbor_already_exists", "vrf": vrf_norm, "neighbor_ip": ip})
    if _satellite_style_vrf_name(vrf_norm) and bgp_ipvlan_reconcile.enabled():
        if not sip:
            raise HTTPException(status_code=400, detail="bgp_ipvlan_source_unknown")
        storage.ensure_bgp_neighbor_meta_row(conn, vrf_norm, ip)
        storage.set_bgp_neighbor_meta(conn, vrf_norm, ip, role, "", update_source=sip or "")
        _bgp_ipvlan_reconcile_vrf_required(vrf_norm, peer_ip=ip)
        bind_if = bgp_ipvlan_reconcile.ipvlan_iface_for_vrf(DB_PATH, vrf_norm) or ""
        if sip and bgp_ipvlan_reconcile.is_rr_spoof_ip(sip):
            passive = bgp_ipvlan_reconcile.rr_spoof_passive_enabled()
    try:
        bgp_control.add_neighbor(
            vrf_norm,
            ip,
            int(body.remote_as),
            role,
            sip or "",
            mh or 0,
            bind_interface=bind_if,
            passive_mode=passive,
        )
        storage.set_bgp_neighbor_meta(conn, vrf_norm, ip, role, "", update_source=sip or "")
        storage.update_bgp_neighbor_advertise_routes(
            conn, vrf_norm, ip, 0, ""
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"gobgp_neighbor_failed: {e}") from e
    if sip and _satellite_style_vrf_name(vrf_norm) and bgp_ipvlan_reconcile.enabled():
        _bgp_ipvlan_reconcile_vrf_required(vrf_norm, peer_ip=ip)
    _arp_reconcile_host_ip_best_effort()
    row = _agent_neighbor_row(conn, vrf_norm, ip)
    if row:
        return _neighbor_out_from_agent(conn, row)
    return _neighbor_out_from_agent(
        conn, {"vrf": vrf_norm, "address": ip, "remote_as": body.remote_as, "local_address": sip, "state": "Active", "enabled": True}
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/global", response_model=GlobalOut)
def api_global_get():
    conn = _db()
    try:
        g = storage.get_global(conn)
        return GlobalOut(hijack_enabled=g.hijack_enabled)
    finally:
        conn.close()


@app.put("/api/global", response_model=GlobalOut)
def api_global_put(body: GlobalIn):
    conn = _db()
    try:
        # 先下发 nft / 再落库，避免 nft 失败时库已改、前端状态与 toast 不一致
        _apply_nft(conn, hijack_enabled=body.hijack_enabled)
        storage.set_global(conn, body.hijack_enabled)
        # 实验室 ICMP TE 改写走 iptables FORWARD → te_rewrite_nfqueue，与 nft TE SNAT 并行；
        # 总开关关闭时必须清空 TE 映射并重启守护进程，否则会仍按 hop 规则替换。
        _sync_te_rewrite_best_effort(conn)
        return GlobalOut(hijack_enabled=body.hijack_enabled)
    finally:
        conn.close()


@app.get("/api/hop-rules", response_model=List[HopRuleOut])
def api_hop_rules_list():
    conn = _db()
    try:
        return [_hop_rule_out(x) for x in storage.list_hop_replace_rules(conn)]
    finally:
        conn.close()


@app.post("/api/hop-rules", response_model=HopRuleOut)
def api_hop_rules_post(body: HopRuleIn):
    conn = _db()
    try:
        row = storage.add_hop_rule(
            conn,
            match_cidr=body.match_cidr,
            forged_src=body.forged_src,
            priority=body.priority,
            enabled=body.enabled,
            note=body.note,
        )
        try:
            _apply_nft(conn)
        except Exception:
            logger.exception("nft sync after hop rule add failed")
        _sync_te_rewrite_best_effort(conn)
        return _hop_rule_out(row)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        conn.close()


@app.patch("/api/hop-rules/{rid}", response_model=HopRuleOut)
def api_hop_rules_patch(rid: int, body: HopRulePatch):
    conn = _db()
    try:
        row = storage.update_hop_rule(
            conn,
            rid,
            match_cidr=body.match_cidr,
            forged_src=body.forged_src,
            priority=body.priority,
            enabled=body.enabled,
            note=body.note,
        )
        if not row:
            raise HTTPException(status_code=404, detail="not_found")
        try:
            _apply_nft(conn)
        except Exception:
            logger.exception("nft sync after hop rule patch failed")
        _sync_te_rewrite_best_effort(conn)
        return _hop_rule_out(row)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        conn.close()


@app.delete("/api/hop-rules/{rid}")
def api_hop_rules_delete(rid: int):
    conn = _db()
    try:
        if not storage.delete_hop_rule(conn, rid):
            raise HTTPException(status_code=404, detail="not_found")
        try:
            _apply_nft(conn)
        except Exception:
            logger.exception("nft sync after hop rule delete failed")
        _sync_te_rewrite_best_effort(conn)
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/arp-spoof/settings", response_model=ArpSpoofSettingsOut)
def api_arp_spoof_settings_get():
    conn = _db()
    try:
        s = storage.get_arp_spoof_settings(conn)
        return ArpSpoofSettingsOut(**s.__dict__)
    finally:
        conn.close()


@app.put("/api/arp-spoof/settings", response_model=ArpSpoofSettingsOut)
def api_arp_spoof_settings_put(body: ArpSpoofSettingsIn):
    conn = _db()
    try:
        settings = storage.set_arp_spoof_settings(conn, arp_spoof_enabled=body.arp_spoof_enabled)
        _bgp_ipvlan_reconcile_best_effort()
        _arp_reconcile_host_ip_best_effort()
        _satellite_vrf_reconcile_best_effort()
        _signal_arp_daemon_reload()
        return ArpSpoofSettingsOut(**settings.__dict__)
    finally:
        conn.close()


@app.get("/api/arp-spoof/targets", response_model=List[ArpTargetOut])
def api_arp_spoof_targets_list():
    conn = _db()
    try:
        rows = storage.list_arp_spoof_targets(conn)
        return [
            ArpTargetOut(
                id=r.id,
                enabled=r.enabled,
                spoof_gateway_ip=r.spoof_gateway_ip,
                satellite_vrf=r.satellite_vrf or None,
                egress_iface=r.egress_iface,
                policy_mode=r.policy_mode,
                policy_cidrs=r.policy_cidrs,
                note=r.note,
                created_at=r.created_at or "",
            )
            for r in rows
        ]
    finally:
        conn.close()


@app.post("/api/arp-spoof/targets", response_model=ArpTargetOut)
def api_arp_spoof_targets_post(body: ArpTargetIn):
    conn = _db()
    try:
        sat_vrf = _arp_auto_satellite_vrf(body.spoof_gateway_ip, body.satellite_vrf, body.note or "")
        row_id = storage.insert_arp_spoof_target(
            conn,
            spoof_gateway_ip=body.spoof_gateway_ip,
            satellite_vrf=sat_vrf or body.satellite_vrf,
            egress_iface=body.egress_iface,
            enabled=body.enabled,
            policy_mode=body.policy_mode,
            policy_cidrs=body.policy_cidrs,
            note=body.note,
        )
        _arp_target_after_write_reconcile(conn, vrf_hint=sat_vrf)
        _signal_arp_daemon_reload()
        row = storage.get_arp_spoof_target(conn, row_id)
        if not row:
            raise HTTPException(status_code=500, detail="Failed to retrieve inserted row")
        return ArpTargetOut(
            id=row.id,
            enabled=row.enabled,
            spoof_gateway_ip=row.spoof_gateway_ip,
            satellite_vrf=row.satellite_vrf or None,
            egress_iface=row.egress_iface,
            policy_mode=row.policy_mode,
            policy_cidrs=row.policy_cidrs,
            note=row.note,
            created_at=row.created_at or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except sqlite3.IntegrityError as e:
        msg = str(e).lower()
        if "unique" in msg or "spoof_gateway_ip" in msg:
            raise HTTPException(status_code=409, detail="spoof_gateway_ip_already_exists") from e
        if "created_at" in msg:
            raise HTTPException(
                status_code=500,
                detail="arp_spoof_targets.created_at schema mismatch; redeploy OP app/storage.py and restart",
            ) from e
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        conn.close()


@app.patch("/api/arp-spoof/targets/{rid}", response_model=ArpTargetOut)
def api_arp_spoof_targets_patch(rid: int, body: ArpTargetPatch):
    conn = _db()
    try:
        ok = storage.update_arp_spoof_target(
            conn,
            rid,
            spoof_gateway_ip=body.spoof_gateway_ip,
            satellite_vrf=body.satellite_vrf,
            egress_iface=body.egress_iface,
            enabled=body.enabled,
            policy_mode=body.policy_mode,
            policy_cidrs=body.policy_cidrs,
            note=body.note,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="not_found")
        row = storage.get_arp_spoof_target(conn, rid)
        if not row:
            raise HTTPException(status_code=404, detail="not_found")
        sat_vrf = _arp_auto_satellite_vrf(row.spoof_gateway_ip, row.satellite_vrf, row.note or "")
        if sat_vrf and sat_vrf != (row.satellite_vrf or "").strip():
            storage.update_arp_spoof_target(conn, rid, satellite_vrf=sat_vrf)
            row = storage.get_arp_spoof_target(conn, rid) or row
        _arp_target_after_write_reconcile(conn, vrf_hint=sat_vrf or (row.satellite_vrf or ""))
        _signal_arp_daemon_reload()
        return ArpTargetOut(
            id=row.id,
            enabled=row.enabled,
            spoof_gateway_ip=row.spoof_gateway_ip,
            satellite_vrf=row.satellite_vrf or None,
            egress_iface=row.egress_iface,
            policy_mode=row.policy_mode,
            policy_cidrs=row.policy_cidrs,
            note=row.note,
            created_at=row.created_at or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        conn.close()


@app.delete("/api/arp-spoof/targets/{rid}")
def api_arp_spoof_targets_delete(rid: int):
    conn = _db()
    try:
        row = storage.get_arp_spoof_target(conn, rid)
        if not row:
            raise HTTPException(status_code=404, detail="not_found")
        spoof_ip = row.spoof_gateway_ip
        vrf_norm = (row.satellite_vrf or "").strip() or _arp_auto_satellite_vrf(
            spoof_ip, row.satellite_vrf, row.note or ""
        )
        removed_bgp: List[Dict[str, Any]] = []
        if vrf_norm and _arp_delete_remove_bgp_enabled() and _satellite_style_vrf_name(vrf_norm):
            removed_bgp = _remove_bgp_neighbors_for_vrf(conn, vrf_norm)
        if not storage.delete_arp_spoof_target(conn, rid):
            raise HTTPException(status_code=404, detail="not_found")
        _arp_target_after_write_reconcile(conn)
        if not bgp_ipvlan_reconcile.enabled():
            _satellite_vrf_reconcile_best_effort()
        _signal_arp_daemon_reload()
        return {"ok": True, "removed_bgp_neighbors": removed_bgp}
    finally:
        conn.close()


@app.post("/api/arp-spoof/satellite-vrfs/reconcile")
def api_arp_spoof_satellite_vrf_reconcile():
    """按当前库与 ``MTR_AUTO_SATELLITE_*`` 环境变量，执行卫星 VRF reconcile（需 root；返回 JSON 摘要）。"""
    return {
        "legacy_veth": satellite_vrf_assign.reconcile_from_op_database(DB_PATH),
        "ipvlan_l2": bgp_ipvlan_reconcile.reconcile_from_op_database(DB_PATH),
    }


@app.post("/api/bgp/ipvlan-satellites/reconcile")
def api_bgp_ipvlan_satellites_reconcile():
    """按 ``docs/bgp-ipvlan-setup.md`` 的 ipvlan L2 架构，手动收敛 Linux 200 的卫星 BGP VRF。"""
    return bgp_ipvlan_reconcile.reconcile_from_op_database(DB_PATH)


@app.get("/api/host-ifaces")
def api_host_ifaces():
    return {"ifaces": _list_net_ifaces_linux()}


@app.get("/api/bgp/neighbor-form-hints")
def api_bgp_neighbor_form_hints():
    """BGP 邻居表单辅助：ARP 引流已配置的「冒充网关」IPv4，供 source_ip 下拉。"""
    conn = _db()
    try:
        rows = storage.list_arp_spoof_targets_enabled(conn)
        ips = sorted({str(r.spoof_gateway_ip or "").strip() for r in rows if str(r.spoof_gateway_ip or "").strip()})
        out = {"arp_spoof_gateway_ips": ips}
        out.update(bgp_control.production_form_hints())
        out["advertise_source_options"] = ["@upstream", "@downstream"]
        env = bgp_control.agent_env_config()
        if env.get("rr_addr"):
            out["advertise_source_options"].append(str(env["rr_addr"]))
        for nip in storage.list_bgp_distinct_learned_neighbor_ips(conn):
            if nip and nip not in out["advertise_source_options"]:
                out["advertise_source_options"].append(nip)
        return out
    finally:
        conn.close()


@app.get("/api/bgp/satellite-vrfs")
def api_bgp_satellite_vrfs():
    """获取所有已配置的satellite_vrf名称列表，用于下拉选择。"""
    conn = _db()
    try:
        vrfs = storage.list_satellite_vrf_names(conn)
        return {"vrfs": vrfs}
    finally:
        conn.close()


@app.get("/api/bgp/vrfs", response_model=List[BgpVrfOut])
def api_bgp_vrfs():
    """GoBGP 架构：VRF 来自 meta / 卫星配置 / 内核 ``ip link``。"""
    conn = _db()
    try:
        las = bgp_control.default_local_as()
        seen = set()
        out: List[BgpVrfOut] = []
        for v in bgp_control.list_vrfs_from_meta(conn):
            if v in seen:
                continue
            seen.add(v)
            out.append(BgpVrfOut(vrf=v, local_as=las, has_router_bgp=True))
        for raw in kernel_vrf.list_kernel_vrf_names():
            try:
                vn = storage.validate_vrf_name(raw)
            except ValueError:
                continue
            if vn in seen:
                continue
            seen.add(vn)
            out.append(BgpVrfOut(vrf=vn, local_as=las, has_router_bgp=False))
        out.sort(key=lambda x: (0 if x.vrf == "default" else 1, x.vrf))
        return out
    finally:
        conn.close()


@app.post("/api/bgp/instances")
def api_bgp_instances_ensure(body: BgpEnsureInstanceIn):
    """创建内核 VRF（GoBGP 按 VRF 懒启动 TX，无需 FRR router bgp）。"""
    try:
        vrf_norm = storage.validate_vrf_name(body.vrf)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _ensure_kernel_vrf_if_missing(vrf_norm, body.create_kernel_vrf_if_missing, body.kernel_rt_table)
    return {"ok": True, "vrf": vrf_norm, "local_as": bgp_control.default_local_as()}


@app.get("/api/bgp/neighbors", response_model=List[BgpNeighborOut])
def api_bgp_neighbors_list(vrf: Optional[str] = Query(None)):
    """从 GoBGP Agent（RX/TX）读取邻居列表，与 SQLite meta 合并展示。"""
    conn = _db()
    try:
        q = (vrf or "").strip()
        try:
            rows = bgp_control.list_agent_neighbors()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"bgp_agent_unavailable: {e}") from e
        out: List[BgpNeighborOut] = []
        seen_rr: set[str] = set()
        for row in rows:
            v = storage.validate_vrf_name(str(row.get("vrf") or "default"))
            nip = str(row.get("address") or "").strip()
            if q and v != storage.validate_vrf_name(q):
                continue
            role, _ = _resolve_bgp_role(conn, v, nip) if nip else ("unknown", "unset")
            sess = str(row.get("session") or "").lower()
            if bgp_control.is_rr_role(role) or v == bgp_control.GOBGP_VRF_RR:
                if sess != "rx":
                    continue
                if nip in seen_rr:
                    continue
                seen_rr.add(nip)
            out.append(_neighbor_out_from_agent(conn, row))
        for vrf_m, nip_m, meta in _iter_rr_meta_rows(conn):
            if nip_m in seen_rr:
                continue
            if q and vrf_m != storage.validate_vrf_name(q):
                continue
            rx_row = next(
                (x for x in bgp_control.list_rr_rx_neighbor_rows() if str(x.get("address") or "").strip() == nip_m),
                None,
            )
            if rx_row:
                out.append(_neighbor_out_from_agent(conn, rx_row))
            else:
                out.append(
                    _neighbor_out_from_agent(
                        conn,
                        {
                            "vrf": vrf_m,
                            "address": nip_m,
                            "remote_as": meta.get("remote_as") or bgp_control.default_local_as(),
                            "state": "IDLE",
                            "enabled": True,
                            "pfx_rcd": 0,
                            "pfx_adv": 0,
                            "local_address": meta.get("source_ip") or "",
                        },
                    )
                )
            seen_rr.add(nip_m)
        return out
    finally:
        conn.close()


@app.post("/api/bgp/sync-from-frr")
def api_bgp_sync_from_frr():
    """从 bgp-agent 同步邻居到 SQLite meta，并写入预设角色（URL 保留 ``sync-from-frr`` 仅为兼容，不调用 vtysh/FRR）。"""
    conn = _db()
    try:
        applied = _seed_bgp_neighbors_from_frr(conn)
        rec = bgp_control.reconcile_meta_to_agent(conn)
        return {
            "ok": True,
            "detail": "synced from gobgp agent + DB role presets",
            "presets_applied": applied,
            "agent_reconcile": rec,
        }
    finally:
        conn.close()


@app.post("/api/bgp/neighbors", response_model=BgpNeighborOut)
def api_bgp_neighbors_add(body: BgpNeighborIn):
    conn = _db()
    try:
        return _bgp_add_neighbor_impl(conn, body)
    finally:
        conn.close()


@app.patch("/api/bgp/neighbors/{vrf}/{neighbor_ip}", response_model=BgpNeighborOut)
def api_bgp_neighbors_patch(vrf: str, neighbor_ip: str, body: BgpNeighborPatch):
    conn = _db()
    try:
        vrf_norm = storage.validate_vrf_name(vrf)
        ip = storage.validate_ipv4(neighbor_ip)
        meta = storage.get_bgp_neighbor_meta_map(conn, vrf_norm).get(ip)
        if not meta:
            raise HTTPException(status_code=404, detail="neighbor_not_found")
        cur_role = meta[0] or "unknown"
        cur_note = meta[1] or ""
        cur_src = meta[2] if len(meta) > 2 else ""
        new_ip = storage.validate_ipv4(body.neighbor_ip) if body.neighbor_ip else ip
        row = _agent_neighbor_row(conn, vrf_norm, ip) or {}
        new_ras = int(body.remote_as) if body.remote_as is not None else int(row.get("remote_as") or bgp_control.default_local_as())
        new_role = body.role.strip().lower() if body.role else cur_role
        new_src = (body.source_ip or "").strip() if body.source_ip is not None else cur_src
        if not bgp_control.is_rr_role(cur_role):
            try:
                bgp_control.remove_neighbor(vrf_norm, ip)
            except Exception as e:
                logger.warning("patch remove %s: %s", ip, e)
        storage.delete_bgp_neighbor_meta(conn, vrf_norm, ip)
        add_in = BgpNeighborIn(
            vrf=vrf_norm,
            neighbor_ip=new_ip,
            remote_as=new_ras,
            role=new_role,
            source_ip=new_src or None,
            note=cur_note,
            create_kernel_vrf_if_missing=False,
        )
        return _bgp_add_neighbor_impl(conn, add_in)
    finally:
        conn.close()


@app.delete("/api/bgp/neighbors/{vrf}/{neighbor_ip}")
def api_bgp_neighbors_delete(vrf: str, neighbor_ip: str):
    conn = _db()
    try:
        vrf_norm = storage.validate_vrf_name(vrf)
        nip = storage.validate_ipv4(neighbor_ip)
        meta = storage.get_bgp_neighbor_meta_map(conn, vrf_norm).get(nip)
        role = (meta[0] if meta else "unknown") or "unknown"
        if bgp_control.is_rr_role(role):
            try:
                bgp_control.remove_rr(nip)
            except Exception as e:
                logger.warning("delete rr %s: %s", nip, e)
            try:
                bgp_control.remove_neighbor(bgp_control.GOBGP_VRF_RR, nip)
            except Exception as e:
                logger.debug("delete stray tx on gobgp-rr: %s", e)
        else:
            try:
                bgp_control.remove_neighbor(vrf_norm, nip)
            except Exception as e:
                logger.warning("delete neighbor %s: %s", nip, e)
        storage.delete_bgp_neighbor_meta(conn, vrf_norm, nip)
        deleted_routes = storage.delete_bgp_learned_routes_by_neighbor_ip(conn, nip)
        if _satellite_style_vrf_name(vrf_norm) and bgp_ipvlan_reconcile.enabled():
            _bgp_ipvlan_reconcile_vrf_best_effort(vrf_norm)
        return {"ok": True, "deleted_routes": deleted_routes}
    finally:
        conn.close()


@app.post("/api/bgp/neighbors/{vrf}/{neighbor_ip}/toggle", response_model=BgpNeighborOut)
def api_bgp_neighbors_toggle(vrf: str, neighbor_ip: str, body: BgpNeighborToggleIn):
    conn = _db()
    try:
        vrf_norm = storage.validate_vrf_name(vrf)
        nip = storage.validate_ipv4(neighbor_ip)
        meta = storage.get_bgp_neighbor_meta_map(conn, vrf_norm).get(nip)
        role = (meta[0] if meta else "unknown") or "unknown"
        try:
            if bgp_control.is_rr_role(role):
                sip = (meta[2] if meta and len(meta) > 2 else "") or bgp_control.default_router_id()
                if body.enabled:
                    ras = bgp_control.default_local_as()
                    rx_row = bgp_control.get_rr_rx_neighbor_row()
                    if rx_row and str(rx_row.get("address") or "").strip() == nip:
                        ras = int(rx_row.get("remote_as") or ras)
                    bgp_control.configure_rr(nip, ras, local_address=sip)
                else:
                    bgp_control.set_rr_enabled(nip, False)
            else:
                bgp_control.set_neighbor_enabled(vrf_norm, nip, bool(body.enabled))
        except Exception as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        row = _agent_neighbor_row(conn, vrf_norm, nip)
        if row:
            row["enabled"] = bool(body.enabled)
            return _neighbor_out_from_agent(conn, row)
        raise HTTPException(status_code=404, detail="neighbor_not_found")
    finally:
        conn.close()



class AdvertiseStatusOut(BaseModel):
    task_id: str
    status: str
    progress: int
    total_routes: int
    added: int
    message: str


@app.post("/api/bgp/neighbors/{vrf}/{neighbor_ip}/advertise", response_model=BgpNeighborOut)
async def api_bgp_neighbors_advertise(vrf: str, neighbor_ip: str, body: BgpNeighborAdvertiseIn):
    """
    路由通告开关：从 Agent 持久库读出路由并通告给本行对端。
    下游 peer：默认读上游 RR 入库来源（本行 TCP 源 / ``10.133.153.204`` 等）再向 ``neighbor_ip`` 推送。
    """
    conn = _db()
    try:
        vrf_norm = storage.validate_vrf_name(vrf)
        ip = storage.validate_ipv4(neighbor_ip)
        role, _rs = _resolve_bgp_role(conn, vrf_norm, ip)

        storage.update_bgp_neighbor_advertise_routes(conn, vrf_norm, ip, body.advertise_routes, "")

        task_id = f"{vrf_norm}-{ip}-advertise"
        async with _ADVERTISE_LOCK:
            if task_id in _ADVERTISE_TASKS and _ADVERTISE_TASKS[task_id].get("status") == "running":
                raise HTTPException(status_code=409, detail="advertise_task_running")
            _ADVERTISE_TASKS.pop(task_id, None)

        from . import bgp_peer_rib

        if body.advertise_routes:
            src_v, src_n, src_r = _advertise_rib_source(conn, vrf_norm, ip, role)
            cached = bgp_peer_rib.count_peer_rib_routes(src_v, src_n, src_r)
            async with _ADVERTISE_LOCK:
                _ADVERTISE_TASKS[task_id] = {
                    "status": "running",
                    "progress": 0,
                    "total_routes": cached,
                    "added": 0,
                    "message": f"通告本窗缓存 {cached} 条…",
                }
            asyncio.create_task(_async_apply_bgp_peer_cache_advertise(vrf_norm, ip, role, task_id))
        else:
            src_v, src_n, src_r = _advertise_rib_source(conn, vrf_norm, ip, role)
            cached = bgp_peer_rib.count_peer_rib_routes(src_v, src_n, src_r)
            async with _ADVERTISE_LOCK:
                _ADVERTISE_TASKS[task_id] = {
                    "status": "running",
                    "progress": 0,
                    "total_routes": cached,
                    "added": 0,
                    "message": f"撤销来自 {src_n} 的 {cached} 条通告…",
                }
            asyncio.create_task(_async_withdraw_bgp_peer_cache_advertise(vrf_norm, ip, role, task_id))
            logger.info("advertise disabled vrf=%s peer=%s", vrf_norm, ip)

        for row in bgp_control.list_agent_neighbors():
            if str(row.get("address")) == ip and storage.validate_vrf_name(
                str(row.get("vrf") or "default")
            ) == vrf_norm:
                return _neighbor_out_from_agent(conn, row)
        role2, _rs = _resolve_bgp_role(conn, vrf_norm, ip)
        meta_t = storage.get_bgp_neighbor_meta_map(conn, vrf_norm).get(ip)
        note_t = (meta_t[1] if meta_t else "") or ""
        src_t = (meta_t[2] if meta_t and len(meta_t) > 2 else "") or ""
        ar_t = int(meta_t[3]) if meta_t and len(meta_t) > 3 else body.advertise_routes
        sent = 0
        if ar_t:
            try:
                src_v, src_n, src_r = _advertise_rib_source(conn, vrf_norm, ip, role2)
                sent = bgp_peer_rib.count_peer_rib_routes(src_v, src_n, src_r)
            except Exception:
                sent = 0
        return BgpNeighborOut(
            vrf=vrf_norm,
            neighbor_ip=ip,
            remote_as=bgp_control.default_local_as(),
            role=role2,
            note=note_t,
            source_ip=src_t,
            local_as=bgp_control.default_local_as(),
            enabled=True,
            session_state="Unknown",
            routes_received=0,
            routes_sent=sent,
            advertise_routes=ar_t,
        )
    finally:
        conn.close()


@app.post("/api/bgp/neighbors/{vrf}/{neighbor_ip}/store-routes", response_model=BgpNeighborOut)
async def api_bgp_neighbors_store_routes(vrf: str, neighbor_ip: str, body: BgpNeighborStoreIn):
    """开关：是否将从对端收到的路由持久化到 Agent（Redis/RocksDB）。"""
    from . import bgp_peer_rib

    conn = _db()
    try:
        vrf_norm = storage.validate_vrf_name(vrf)
        ip = storage.validate_ipv4(neighbor_ip)
        role, _ = _resolve_bgp_role(conn, vrf_norm, ip)
        storage.ensure_bgp_neighbor_meta_row(conn, vrf_norm, ip)
        storage.update_bgp_neighbor_store_received_routes(conn, vrf_norm, ip, body.store_received_routes)
        await _run_blocking_call(
            bgp_peer_rib.sync_peer_rib_policy, vrf_norm, ip, role, body.store_received_routes
        )
        if body.store_received_routes:
            await _run_blocking_call(bgp_peer_rib.ingest_peer_routes, vrf_norm, ip, role)
        for row in bgp_control.list_agent_neighbors():
            if str(row.get("address")) == ip and storage.validate_vrf_name(
                str(row.get("vrf") or "default")
            ) == vrf_norm:
                return _neighbor_out_from_agent(conn, row)
        return BgpNeighborOut(
            vrf=vrf_norm,
            neighbor_ip=ip,
            remote_as=bgp_control.default_local_as(),
            role=role,
            local_as=bgp_control.default_local_as(),
            enabled=True,
            session_state="Unknown",
            store_received_routes=body.store_received_routes,
        )
    finally:
        conn.close()


@app.get("/api/bgp/neighbors/{vrf}/{neighbor_ip}/advertise/status", response_model=AdvertiseStatusOut)
async def api_bgp_neighbors_advertise_status(vrf: str, neighbor_ip: str):
    """查询路由通告任务状态"""
    task_id = f"{vrf}-{neighbor_ip}-advertise"
    async with _ADVERTISE_LOCK:
        status = _ADVERTISE_TASKS.get(task_id)
        if not status:
            return AdvertiseStatusOut(
                task_id=task_id,
                status="idle",
                progress=0,
                total_routes=0,
                added=0,
                message="No active task"
            )
        return AdvertiseStatusOut(
            task_id=task_id,
            status=status["status"],
            progress=status["progress"],
            total_routes=status["total_routes"],
            added=status["added"],
            message=status["message"]
        )


def _advertise_target_is_rr(conn: sqlite3.Connection, vrf_norm: str, target_neighbor: str, role: str) -> bool:
    if vrf_norm == bgp_control.GOBGP_VRF_RR:
        return True
    if bgp_control.is_rr_role(role):
        return True
    meta_row = storage.get_bgp_neighbor_meta_map(conn, vrf_norm).get(target_neighbor)
    return bool(meta_row and bgp_control.is_rr_role(str(meta_row[0] or "")))


def _default_rr_neighbor_ip(conn: sqlite3.Connection) -> str:
    env_rr = (os.environ.get("RR_ADDR") or "").strip()
    if env_rr:
        return storage.validate_ipv4(env_rr)
    for nip, meta in storage.get_bgp_neighbor_meta_map(conn, bgp_control.GOBGP_VRF_RR).items():
        if meta and bgp_control.is_rr_role(str(meta[0] or "")):
            return storage.validate_ipv4(nip)
    raise ValueError("no RR neighbor configured")


def _advertise_rib_source(
    conn: sqlite3.Connection, vrf_norm: str, target_neighbor: str, role: str
) -> Tuple[str, str, str]:
    """读持久库用的 (vrf, neighbor_ip, role)。下游默认取上游 RR 入库来源（TCP 源 IP 对应 peer）。"""
    if _advertise_target_is_rr(conn, vrf_norm, target_neighbor, role):
        return vrf_norm, target_neighbor, role

    meta = storage.get_bgp_neighbor_meta_map(conn, vrf_norm).get(target_neighbor)
    from_spec = str(meta[4] or "").strip() if meta and len(meta) > 4 else ""
    if from_spec:
        low = from_spec.lower()
        if low in {"@upstream", "upstream", "rr"}:
            return bgp_control.GOBGP_VRF_RR, _default_rr_neighbor_ip(conn), "rr"
        if low not in {"@downstream", "downstream"}:
            try:
                return bgp_control.GOBGP_VRF_RR, storage.validate_ipv4(from_spec), "rr"
            except ValueError:
                pass

    source_ip = str(meta[2] or "").strip() if meta and len(meta) > 2 else ""
    if source_ip:
        return bgp_control.GOBGP_VRF_RR, storage.validate_ipv4(source_ip), "rr"

    return bgp_control.GOBGP_VRF_RR, _default_rr_neighbor_ip(conn), "rr"


async def _poll_agent_rib_advertise_job(
    task_id: str,
    src_vrf: str,
    src_nip: str,
    src_role: str,
    vrf_norm: str,
    target_neighbor: str,
    target_to_rr: bool,
    *,
    enable: bool,
    poll_interval: float = 1.5,
    timeout_s: float = 7200.0,
) -> None:
    """Agent 流式通告/撤销：IteratePeerRoutes + 批量 AddPath，OP 仅轮询进度。"""
    from . import bgp_peer_rib

    src_window = storage.route_window_for_bgp_role(src_role)
    target = "rr" if target_to_rr else "tx"
    target_vrf = vrf_norm if not target_to_rr else ""

    await _run_blocking_call(
        bgp_peer_rib.start_rib_advertise_job,
        task_id,
        src_window,
        src_vrf,
        src_nip,
        target=target,
        target_vrf=target_vrf,
        enable=enable,
    )

    import time

    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        st = await _run_blocking_call(bgp_peer_rib.get_rib_advertise_status, task_id)
        status = str(st.get("status") or "")
        total = int(st.get("total_routes") or 0)
        processed = int(st.get("processed") or 0)
        added = int(st.get("added") or 0)
        progress = int(st.get("progress") or 0)
        message = str(st.get("message") or "")

        async with _ADVERTISE_LOCK:
            if task_id in _ADVERTISE_TASKS:
                if total:
                    _ADVERTISE_TASKS[task_id]["total_routes"] = total
                _ADVERTISE_TASKS[task_id]["progress"] = progress
                _ADVERTISE_TASKS[task_id]["added"] = added
                _ADVERTISE_TASKS[task_id]["message"] = message or (
                    f"{'通告' if enable else '撤销'} {processed}/{total or '?'}"
                )

        if status in {"completed", "error"}:
            async with _ADVERTISE_LOCK:
                if task_id in _ADVERTISE_TASKS:
                    _ADVERTISE_TASKS[task_id]["status"] = status
                    if status == "completed":
                        _ADVERTISE_TASKS[task_id]["progress"] = 100
                    _ADVERTISE_TASKS[task_id]["message"] = message
            if status == "error":
                raise RuntimeError(message or "rib_advertise_failed")
            return

        await asyncio.sleep(poll_interval)

    raise TimeoutError(f"rib advertise job timeout after {timeout_s}s")


async def _async_apply_bgp_peer_cache_advertise(
    vrf: str, target_neighbor: str, role: str, task_id: str
) -> None:
    """从持久库（下游默认上游 RR 来源）读出路由，通告给目标对端（Agent 流式任务）。"""
    from . import bgp_peer_rib

    try:
        conn = _db()
        try:
            vrf_norm = storage.validate_vrf_name(vrf)
            target_to_rr = _advertise_target_is_rr(conn, vrf_norm, target_neighbor, role)
            src_vrf, src_nip, src_role = _advertise_rib_source(conn, vrf_norm, target_neighbor, role)
            total_routes_count = await _run_blocking_call(
                bgp_peer_rib.count_peer_rib_routes, src_vrf, src_nip, src_role
            )

            async with _ADVERTISE_LOCK:
                if task_id in _ADVERTISE_TASKS:
                    _ADVERTISE_TASKS[task_id]["total_routes"] = total_routes_count
                    _ADVERTISE_TASKS[task_id]["message"] = (
                        f"来源 {src_vrf}/{src_nip} 共 {total_routes_count} 条 → "
                        f"{'RR(RX)' if target_to_rr else f'TX {vrf_norm}→{target_neighbor}'}"
                    )

            if total_routes_count == 0:
                async with _ADVERTISE_LOCK:
                    if task_id in _ADVERTISE_TASKS:
                        _ADVERTISE_TASKS[task_id]["status"] = "completed"
                        _ADVERTISE_TASKS[task_id]["message"] = (
                            f"来源 {src_vrf}/{src_nip} 无路由，请先在 RR 侧打开路由入库"
                        )
                return

            await _poll_agent_rib_advertise_job(
                task_id,
                src_vrf,
                src_nip,
                src_role,
                vrf_norm,
                target_neighbor,
                target_to_rr,
                enable=True,
            )
            added = 0
            async with _ADVERTISE_LOCK:
                if task_id in _ADVERTISE_TASKS:
                    added = int(_ADVERTISE_TASKS[task_id].get("added") or 0)
            logger.info(
                "Advertise task %s done added=%s total=%s src=%s/%s target=%s/%s rr=%s",
                task_id,
                added,
                total_routes_count,
                src_vrf,
                src_nip,
                vrf_norm,
                target_neighbor,
                target_to_rr,
            )
        finally:
            conn.close()
    except Exception as e:
        async with _ADVERTISE_LOCK:
            if task_id in _ADVERTISE_TASKS:
                _ADVERTISE_TASKS[task_id]["status"] = "error"
                _ADVERTISE_TASKS[task_id]["message"] = str(e)[:200]
        logger.error("Advertise task %s failed: %s", task_id, e)


async def _async_withdraw_bgp_peer_cache_advertise(
    vrf: str, target_neighbor: str, role: str, task_id: str
) -> None:
    """关闭通告开关：按上游来源持久库前缀向目标对端撤销（Agent 流式任务）。"""
    from . import bgp_peer_rib

    try:
        conn = _db()
        try:
            vrf_norm = storage.validate_vrf_name(vrf)
            target_to_rr = _advertise_target_is_rr(conn, vrf_norm, target_neighbor, role)
            src_vrf, src_nip, src_role = _advertise_rib_source(conn, vrf_norm, target_neighbor, role)
            total_routes_count = await _run_blocking_call(
                bgp_peer_rib.count_peer_rib_routes, src_vrf, src_nip, src_role
            )
            if total_routes_count == 0:
                async with _ADVERTISE_LOCK:
                    if task_id in _ADVERTISE_TASKS:
                        _ADVERTISE_TASKS[task_id]["status"] = "completed"
                        _ADVERTISE_TASKS[task_id]["message"] = "无缓存路由需撤销"
                return

            async with _ADVERTISE_LOCK:
                if task_id in _ADVERTISE_TASKS:
                    _ADVERTISE_TASKS[task_id]["total_routes"] = total_routes_count

            await _poll_agent_rib_advertise_job(
                task_id,
                src_vrf,
                src_nip,
                src_role,
                vrf_norm,
                target_neighbor,
                target_to_rr,
                enable=False,
            )
        finally:
            conn.close()
    except Exception as e:
        async with _ADVERTISE_LOCK:
            if task_id in _ADVERTISE_TASKS:
                _ADVERTISE_TASKS[task_id]["status"] = "error"
                _ADVERTISE_TASKS[task_id]["message"] = str(e)[:200]
        logger.error("Withdraw advertise task %s failed: %s", task_id, e)


def _apply_bgp_route_advertise(vrf: str, target_neighbor: str, source_neighbor_ip: str) -> dict:
    """
    将从 source_neighbor_ip 学到的路由通告给 target_neighbor。
    这是一个同步操作，直接执行批量添加。
    """
    try:
        routes_count = storage.count_bgp_learned_routes_by_neighbor_ip(_db(), source_neighbor_ip)
        if routes_count == 0:
            return {"status": "ok", "added": 0, "message": f"no routes from {source_neighbor_ip}"}

        conn = _db()
        try:
            routes_batch = []
            batch_size = 5000
            total_added = 0

            for prefix, nexthop in storage.iter_bgp_learned_routes_by_neighbor_ip(conn, source_neighbor_ip, batch_size):
                routes_batch.append((prefix, nexthop))

                if len(routes_batch) >= batch_size:
                    result = bgp_control.add_bgp_networks_batch(vrf, routes_batch)
                    total_added += result.get("added", 0)
                    routes_batch = []

            if routes_batch:
                result = bgp_control.add_bgp_networks_batch(vrf, routes_batch)
                total_added += result.get("added", 0)

            return {
                "status": "ok",
                "added": total_added,
                "total_routes": routes_count,
                "source_neighbor": source_neighbor_ip,
                "target_neighbor": target_neighbor,
            }
        finally:
            conn.close()
    except Exception as e:
        logger.error("failed to advertise routes: %s", str(e))
        return {"status": "error", "message": str(e)}


@app.get("/api/bgp/learned-routes/filter-options")
async def api_bgp_learned_routes_filter_options():
    """下拉用：Agent 邻居列表 + 持久库 upstream/downstream 汇总。"""
    from . import bgp_peer_rib

    conn = _db()
    try:
        vrfs: list[str] = []
        ips: list[str] = []
        peer_pairs: list[dict[str, str]] = []
        summary = {"upstream": 0, "downstream": 0, "total": 0}
        seen_vrf: set[str] = set()
        seen_ip: set[str] = set()
        seen_pair: set[tuple[str, str]] = set()

        async def _add_peer(v: str, ip: str, role: str) -> None:
            nonlocal summary
            pair = (v, ip)
            if pair in seen_pair:
                return
            seen_pair.add(pair)
            peer_pairs.append({"vrf": v, "neighbor_ip": ip, "role": role})
            if v not in seen_vrf:
                seen_vrf.add(v)
                vrfs.append(v)
            if ip not in seen_ip:
                seen_ip.add(ip)
                ips.append(ip)
            rw = storage.route_window_for_bgp_role(role)
            try:
                cnt = await _run_blocking_call(bgp_peer_rib.count_peer_rib_routes, v, ip, role)
            except Exception:
                cnt = 0
            if rw == "upstream":
                summary["upstream"] += cnt
            else:
                summary["downstream"] += cnt
            summary["total"] += cnt

        for v, ip, role in _collect_learned_route_peers(conn, None, None, None):
            await _add_peer(v, ip, role)
        vrfs.sort()
        ips.sort()
        return {
            "vrfs": vrfs,
            "neighbor_ips": ips,
            "peer_pairs": peer_pairs,
            "route_windows": ["upstream", "downstream"],
            "summary": summary,
            "peer_snapshots": storage.list_bgp_peer_snapshots_brief(conn),
            "data_source": "rib_agent",
        }
    finally:
        conn.close()


@app.get("/api/bgp/learned-routes", response_model=BgpLearnedRoutesSnapshotOut)
async def api_bgp_learned_routes_list(
    vrf: Optional[str] = Query(None, description="按 VRF 筛选；省略表示全部"),
    neighbor_ip: Optional[str] = Query(None, description="按来源邻居 IP 精确筛选；省略表示全部"),
    route_window: Optional[str] = Query(
        None, description="upstream=RR 窗；downstream=下游窗；省略表示全部"
    ),
    merge_upstream_stale: bool = Query(True, description="合并上游持久缓存中、当前 RIB 快照已缺失的前缀（stale=true）"),
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(100, ge=1, le=1000, description="每页条数，范围 1-1000"),
):
    """从 bgp-agent 持久库（Redis/RocksDB）分页读取学习路由。"""
    from . import bgp_peer_rib

    conn = _db()
    try:
        q_vrf = (vrf or "").strip() or None
        nip_raw = (neighbor_ip or "").strip()
        nip: Optional[str] = None
        if nip_raw:
            try:
                nip = storage.validate_ipv4(nip_raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid neighbor_ip")
        rw_raw = (route_window or "").strip().lower() or None
        if rw_raw and rw_raw not in {"upstream", "downstream"}:
            raise HTTPException(status_code=400, detail="invalid route_window")

        frozen_map = storage.get_bgp_peer_frozen_map(conn)
        peer_snaps = storage.list_bgp_peer_snapshots_brief(conn)
        summary = {"upstream": 0, "downstream": 0, "total": 0}
        peers = _collect_learned_route_peers(conn, q_vrf, nip, rw_raw)

        for v, ip, role in peers:
            rw_use = storage.route_window_for_bgp_role(role)
            try:
                cnt = await _run_blocking_call(bgp_peer_rib.count_peer_rib_routes, v, ip, role)
            except Exception:
                cnt = 0
            if rw_use == "upstream":
                summary["upstream"] += cnt
            else:
                summary["downstream"] += cnt
            summary["total"] += cnt

        routes: list[BgpLearnedRouteOut] = []
        total = 0
        if peers:
            if len(peers) == 1:
                v, ip, role = peers[0]
                rw_use = rw_raw or storage.route_window_for_bgp_role(role)
                data = await _run_blocking_call(
                    bgp_peer_rib.list_peer_rib_routes_page,
                    v,
                    ip,
                    role,
                    page,
                    page_size,
                )
                total = int(data.get("total") or 0)
                raw_routes = data.get("routes") or []
            else:
                data = await _run_blocking_call(
                    bgp_peer_rib.list_merged_rib_routes_page, peers, page, page_size
                )
                total = int(data.get("total") or 0)
                raw_routes = data.get("routes") or []

            for item in raw_routes:
                v = str(item.get("vrf") or "")
                ip = str(item.get("neighbor_ip") or "")
                role, _ = _resolve_bgp_role(conn, v, ip) if v and ip else ("unknown", "unset")
                rw_use = str(item.get("window") or "") or storage.route_window_for_bgp_role(role)
                routes.append(
                    BgpLearnedRouteOut(
                        vrf=v,
                        prefix=str(item.get("prefix") or ""),
                        nexthop=str(item.get("nexthop") or ""),
                        neighbor_ip=ip,
                        remote_as=int(item.get("remote_as") or 0),
                        role=role,
                        as_path=str(item.get("as_path") or ""),
                        updated_at=str(item.get("updated_at") or ""),
                        route_window=rw_use,
                        peer_frozen=frozen_map.get((v, ip), False),
                        persisted=True,
                        stale=False,
                        data_source="rib_agent",
                    )
                )

        st = storage.get_bgp_rib_sync_state(conn)
        return BgpLearnedRoutesSnapshotOut(
            last_sync_at=st[0],
            last_sync_ok=True,
            last_sync_error="",
            routes=routes,
            total=total,
            page=page,
            page_size=page_size,
            route_window=rw_raw,
            summary=summary,
            peer_snapshots=peer_snaps,
        )
    finally:
        conn.close()


@app.post("/api/bgp/learned-routes/ingest")
async def api_bgp_learned_routes_ingest(
    vrf: str = Query(..., description="VRF"),
    neighbor_ip: str = Query(..., description="邻居 IP"),
):
    """从对端 ADJ-RIB-In 全量灌入 Agent 持久库（需已开「路由入库」）。"""
    from . import bgp_peer_rib

    conn = _db()
    try:
        vrf_norm = storage.validate_vrf_name(vrf)
        ip = storage.validate_ipv4(neighbor_ip)
        role, _ = _resolve_bgp_role(conn, vrf_norm, ip)
        if not storage.get_bgp_neighbor_store_received_routes(conn, vrf_norm, ip):
            raise HTTPException(status_code=400, detail="store_received_routes is off for this peer")
        result = await _run_blocking_call(bgp_peer_rib.ingest_peer_routes, vrf_norm, ip, role)
        return {"ok": True, **(result or {})}
    finally:
        conn.close()


@app.post("/api/bgp/learned-routes/sync")
async def api_bgp_learned_routes_sync_now(
    vrf: Optional[str] = Query(None),
    neighbor_ip: Optional[str] = Query(None),
):
    """兼容旧按钮：若带 vrf+neighbor_ip 则执行 ingest；否则仅刷新汇总（不再写 SQLite RIB）。"""
    if (vrf or "").strip() and (neighbor_ip or "").strip():
        return await api_bgp_learned_routes_ingest(vrf=vrf.strip(), neighbor_ip=neighbor_ip.strip())  # type: ignore[arg-type]
    return {"ok": True, "message": "RIB 已改为 Agent 持久库；请选 VRF+邻居后点「立即同步」执行 ingest，或在上游开入库后由 Watch 写入"}


# ----- VPN egress endpoints -----


@app.get("/api/vpn/summary", response_model=VpnSummaryOut)
def api_vpn_summary():
    conn = _db()
    try:
        rows = storage.list_vpn_links(conn)
        up = sum(1 for r in rows if (r.get("actual_status") or "") == "up")
        down = sum(1 for r in rows if (r.get("actual_status") or "") in {"down", "unknown"} and r.get("enabled") and r.get("desired_up"))
        dis = sum(1 for r in rows if not r.get("enabled") or not r.get("desired_up"))
        return VpnSummaryOut(total=len(rows), up=up, down=down, disabled=dis)
    finally:
        conn.close()


@app.get("/api/vpn/links", response_model=List[VpnLinkOut])
def api_vpn_links_list():
    conn = _db()
    try:
        return [_vpn_link_out(x) for x in storage.list_vpn_links(conn)]
    finally:
        conn.close()


@app.post("/api/vpn/links", response_model=VpnLinkOut)
def api_vpn_links_post(body: VpnLinkIn):
    conn = _db()
    try:
        try:
            row = storage.add_vpn_link(
                conn,
                name=body.name,
                link_type=body.link_type,
                vrf=body.vrf,
                endpoint=body.endpoint,
                iface_name=body.iface_name,
                enabled=body.enabled,
                desired_up=body.desired_up,
                priority=body.priority,
                config=body.config,
            )
            storage.append_vpn_event_log(conn, "vpn_api", row["id"], f"created link {body.name}")
            return _vpn_link_out(row)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except sqlite3.IntegrityError as e:
            raise HTTPException(status_code=400, detail="vpn_name_exists") from e
    finally:
        conn.close()


@app.get("/api/vpn/links/{lid}", response_model=VpnLinkOut)
def api_vpn_links_get(lid: int):
    conn = _db()
    try:
        row = storage.get_vpn_link(conn, lid)
        if not row:
            raise HTTPException(status_code=404, detail="not_found")
        return _vpn_link_out(row)
    finally:
        conn.close()


@app.patch("/api/vpn/links/{lid}", response_model=VpnLinkOut)
def api_vpn_links_patch(lid: int, body: VpnLinkPatch):
    conn = _db()
    try:
        data = body.model_dump(exclude_unset=True)
        try:
            row = storage.update_vpn_link(
                conn,
                lid,
                name=data.get("name"),
                link_type=data.get("link_type"),
                vrf=data.get("vrf"),
                endpoint=data.get("endpoint"),
                iface_name=data.get("iface_name"),
                enabled=data.get("enabled"),
                desired_up=data.get("desired_up"),
                priority=data.get("priority"),
                config=data.get("config"),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except sqlite3.IntegrityError as e:
            raise HTTPException(status_code=400, detail="vpn_name_exists") from e
        if not row:
            raise HTTPException(status_code=404, detail="not_found")
        storage.append_vpn_event_log(conn, "vpn_api", lid, "patched link")
        return _vpn_link_out(row)
    finally:
        conn.close()


@app.delete("/api/vpn/links/{lid}")
def api_vpn_links_delete(lid: int):
    conn = _db()
    try:
        try:
            if not storage.delete_vpn_link(conn, lid):
                raise HTTPException(status_code=404, detail="not_found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        storage.append_vpn_event_log(conn, "vpn_api", lid, "deleted link")
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/vpn/apply")
def api_vpn_apply():
    """按库幂等下发 GRE/OpenVPN/L2TP 占位与策略路由（需 Linux + CAP_NET_ADMIN 等）。"""
    conn = _db()
    try:
        out = vpn_egress.apply_all(conn)
        return out
    finally:
        conn.close()


@app.post("/api/vpn/ping")
def api_vpn_ping(body: VpnPingIn):
    t = (body.target or "").strip()
    if not t or len(t) > 253:
        raise HTTPException(status_code=400, detail="invalid_target")
    return vpn_egress.ping_in_vrf(body.vrf, t, body.count)


@app.get("/api/vpn/policies", response_model=List[VpnPolicyOut])
def api_vpn_policies_list():
    conn = _db()
    try:
        return [_vpn_policy_out(x) for x in storage.list_vpn_policies(conn)]
    finally:
        conn.close()


@app.post("/api/vpn/policies", response_model=VpnPolicyOut)
def api_vpn_policies_post(body: VpnPolicyIn):
    conn = _db()
    try:
        try:
            row = storage.add_vpn_policy(
                conn,
                dst_cidr=body.dst_cidr,
                src_cidr=body.src_cidr,
                src_label=body.src_label,
                vpn_link_id=body.vpn_link_id,
                backup_link_id=body.backup_link_id,
                fail_action=body.fail_action,
                enabled=body.enabled,
            )
            storage.append_vpn_event_log(conn, "vpn_policy", row["id"], f"policy dst={body.dst_cidr}")
            return _vpn_policy_out(row)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        conn.close()


@app.patch("/api/vpn/policies/{pid}", response_model=VpnPolicyOut)
def api_vpn_policies_patch(pid: int, body: VpnPolicyPatch):
    conn = _db()
    try:
        data = body.model_dump(exclude_unset=True)
        bk = storage.VPN_UNSET
        if "backup_link_id" in data:
            bk = data["backup_link_id"]
        try:
            row = storage.update_vpn_policy(
                conn,
                pid,
                dst_cidr=data.get("dst_cidr"),
                src_cidr=data.get("src_cidr"),
                src_label=data.get("src_label"),
                vpn_link_id=data.get("vpn_link_id"),
                backup_link_id=bk,
                fail_action=data.get("fail_action"),
                enabled=data.get("enabled"),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not row:
            raise HTTPException(status_code=404, detail="not_found")
        return _vpn_policy_out(row)
    finally:
        conn.close()


@app.delete("/api/vpn/policies/{pid}")
def api_vpn_policies_delete(pid: int):
    conn = _db()
    try:
        if not storage.delete_vpn_policy(conn, pid):
            raise HTTPException(status_code=404, detail="not_found")
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/vpn/events")
def api_vpn_events(limit: int = Query(200, ge=1, le=2000)):
    conn = _db()
    try:
        return {"items": storage.list_vpn_event_log(conn, limit)}
    finally:
        conn.close()


# ===== GoBGP Agent 新架构API =====

@app.get("/api/gobgp/status")
async def api_gobgp_status():
    """获取GoBGP Agent状态"""
    gobgp = gobgp_client.get_gobgp_client()
    try:
        status = await gobgp.get_status()
        rr_status = await gobgp.get_rr_status()
        return {
            "agent": status,
            "rr": rr_status,
            "architecture": "RX/TX分离",
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"GoBGP Agent不可用: {e}")


@app.get("/api/gobgp/routes")
async def api_gobgp_routes(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
):
    """从GoBGP Agent获取BGP学习路由（Redis/RocksDB）"""
    gobgp = gobgp_client.get_gobgp_client()
    try:
        routes = await gobgp.list_routes()
        total = len(routes)
        
        # 分页
        start = (page - 1) * page_size
        end = start + page_size
        paged_routes = routes[start:end]
        
        return {
            "routes": paged_routes,
            "total": total,
            "page": page,
            "page_size": page_size,
            "source": "gobgp_agent_redis_rocksdb",
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"获取路由失败: {e}")


@app.get("/api/gobgp/routes/count")
async def api_gobgp_routes_count():
    """获取BGP学习路由数量"""
    gobgp = gobgp_client.get_gobgp_client()
    try:
        count = await gobgp.get_route_count()
        return {"count": count}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"获取路由数量失败: {e}")


class GoBGPNeighborIn(BaseModel):
    """GoBGP邻居输入模型"""
    address: str = Field(..., description="下游邻居地址")
    remote_as: int = Field(..., ge=1, le=4294967295, description="下游邻居AS号")


@app.post("/api/gobgp/neighbors")
async def api_gobgp_add_neighbor(body: GoBGPNeighborIn):
    """向GoBGP TX Agent添加下游邻居"""
    gobgp = gobgp_client.get_gobgp_client()
    try:
        result = await gobgp.add_neighbor(body.address, body.remote_as)
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"添加邻居失败: {e}")


@app.delete("/api/gobgp/neighbors/{address}")
async def api_gobgp_remove_neighbor(address: str):
    """从GoBGP TX Agent删除下游邻居"""
    gobgp = gobgp_client.get_gobgp_client()
    try:
        result = await gobgp.remove_neighbor(address)
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"删除邻居失败: {e}")


@app.post("/api/gobgp/freeze")
async def api_gobgp_freeze():
    """冻结BGP路由通告（测试RR down场景）"""
    gobgp = gobgp_client.get_gobgp_client()
    try:
        result = await gobgp.freeze()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"冻结失败: {e}")


@app.post("/api/gobgp/unfreeze")
async def api_gobgp_unfreeze():
    """解冻BGP路由通告（恢复RR连接后）"""
    gobgp = gobgp_client.get_gobgp_client()
    try:
        result = await gobgp.unfreeze()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"解冻失败: {e}")


@app.get("/")
def index():
    index_html = STATIC_DIR / "index.html"
    if not index_html.is_file():
        return {"msg": "static/index.html missing"}
    return FileResponse(index_html)
