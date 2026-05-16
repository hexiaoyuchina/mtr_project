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
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from . import arp_spoof_assign, bgp_ipvlan_reconcile, bgp_learned_routes_sync, bgp_sticky_reconcile, frr_bgp, gobgp_client, nft_sync, satellite_vrf_assign, storage, te_rewrite_sync, vpn_egress

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
    """Python 3.8 无 asyncio.to_thread，用线程池执行阻塞 FRR/SQLite 逻辑。"""
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


def _apply_nft(conn: sqlite3.Connection) -> None:
    if os.environ.get("MTR_OP_SKIP_NFT_SYNC", "").strip().lower() in {"1", "true", "yes"}:
        logger.warning("MTR_OP_SKIP_NFT_SYNC is set: skipping nft sync after global/hop changes")
        return
    try:
        _sync_nft_from_conn(conn)
    except Exception as e:
        logger.exception("nft sync failed")
        raise HTTPException(status_code=500, detail=f"nft_sync_failed: {e}") from e


def _sync_te_rewrite_best_effort(conn: sqlite3.Connection) -> None:
    try:
        te_rewrite_sync.sync_te_rewrite_from_conn(conn)
    except Exception:
        logger.exception("te_rewrite sync failed")


async def _bgp_rib_sync_loop() -> None:
    await asyncio.sleep(8)
    interval = int(os.environ.get("MTR_BGP_RIB_SYNC_SEC", "60"))
    while True:
        try:
            await _run_blocking_call(bgp_learned_routes_sync.sync_bgp_learned_routes, DB_PATH)
        except Exception:
            logger.exception("bgp rib periodic sync")
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


def _bgp_ipvlan_reconcile_vrf_best_effort(vrf_norm: str) -> None:
    """新增/修改 BGP 邻居前，尽量先把该卫星 VRF 的 ipvlan 和路由准备好。"""
    try:
        r = bgp_ipvlan_reconcile.reconcile_vrf_from_op_database(DB_PATH, vrf_norm)
        logger.info("bgp_ipvlan_reconcile vrf=%s: %s", vrf_norm, r)
    except Exception:
        logger.exception("bgp_ipvlan_reconcile failed vrf=%s", vrf_norm)


def _bgp_ipvlan_reconcile_vrf_required(vrf_norm: str) -> None:
    """BGP 新增邻居前必须成功准备 ipvlan，否则邻居会写入但 TCP 起不来。"""
    try:
        r = bgp_ipvlan_reconcile.reconcile_vrf_from_op_database(DB_PATH, vrf_norm)
    except Exception as e:
        logger.exception("bgp_ipvlan_reconcile required failed vrf=%s", vrf_norm)
        raise HTTPException(status_code=503, detail=f"bgp_ipvlan_reconcile_failed: {e}") from e
    if r.get("skipped") or r.get("ok") is False:
        raise HTTPException(status_code=503, detail={"code": "bgp_ipvlan_reconcile_failed", "result": r})


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
    """OP 省略 source_ip 时，卫星 VRF 在 underlay 模式下使用 veth 本端 ``10.255.x.1`` 作 FRR update-source。"""
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
    return frr_bgp.ebgp_multihop_satellite_default() if _satellite_style_vrf_name(vrf_norm) else None


def _ensure_satellite_ebgp_multihop(vrf_norm: str, neighbor_ip: str) -> None:
    mh = _satellite_bgp_ebgp_multihop(vrf_norm)
    if not mh:
        if _satellite_style_vrf_name(vrf_norm) and bgp_ipvlan_reconcile.enabled():
            try:
                frr_bgp.set_neighbor_ebgp_multihop(vrf_norm, neighbor_ip, None)
            except frr_bgp.VtyshError as e:
                logger.warning("remove ebgp-multihop failed vrf=%s nbr=%s: %s", vrf_norm, neighbor_ip, e)
        return
    try:
        frr_bgp.set_neighbor_ebgp_multihop(vrf_norm, neighbor_ip, mh)
    except frr_bgp.VtyshError as e:
        logger.warning("set ebgp-multihop failed vrf=%s nbr=%s: %s", vrf_norm, neighbor_ip, e)


def _bgp_auto_create_kernel_vrf_enabled() -> bool:
    return os.environ.get("MTR_BGP_AUTO_CREATE_KERNEL_VRF", "1").strip().lower() not in {"0", "false", "no"}


def _ensure_kernel_vrf_if_missing(vrf_norm: str, create: bool, rt_table: Optional[int]) -> None:
    """非 default：若请求且内核尚无 VRF 设备，则 ``ip link add … type vrf``。"""
    if vrf_norm == "default" or not create or not _bgp_auto_create_kernel_vrf_enabled():
        return
    if vrf_norm in set(frr_bgp.list_kernel_vrf_names()):
        return
    try:
        frr_bgp.ensure_kernel_vrf(vrf_norm, rt_table)
    except frr_bgp.VtyshError as e:
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


def _hop_rule_out(row: dict) -> HopRuleOut:
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
        description="若为 False：内核已有 VRF 设备但 FRR 尚无对应 ``router bgp … vrf …``，新增邻居时可自动建仓或先调 POST /api/bgp/instances",
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
            "本端 TCP/BGP 源地址，下发 FRR ``neighbor … update-source …``；通常与 ARP 引流「冒充网关 IPv4」一致。"
            "卫星 VRF（名前缀同 ``MTR_SATELLITE_VRF_PREFIX``，如 vbgp250）且 ``MTR_SATELLITE_BGP_TCP_SOURCE=underlay``（默认）时，"
            "省略本字段则自动使用卫星 veth 本端 ``10.255.x.1``，以便与 Linux 201 建连；显式填写则沿用该地址。"
        ),
    )
    bgp_local_as: Optional[int] = Field(
        default=None,
        ge=1,
        le=4294967295,
        description="当所选 VRF 尚无 FRR ``router bgp`` 时，用该 AS 自动创建 BGP 实例；省略则用环境或非 default 实例的 AS",
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
        description="若与 URL 中邻居 IP 不同，则在 FRR 中删旧邻、以新 IP 重建；未传的其它字段沿用当前值",
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
    advertise_routes: int = Field(default=0, description="是否启用路由通告：0-禁用，1-启用")
    advertise_routes_from: str = Field(default="", description="从哪个邻居IP学习路由")


class BgpNeighborOut(BaseModel):
    vrf: str
    neighbor_ip: str
    remote_as: int
    role: str
    role_source: str
    note: str = ""
    source_ip: str = ""
    local_as: int
    enabled: bool
    session_state: str
    pfx_rcd: int
    up_down: str
    neighbor_ver: int = 0
    msg_rcvd: int = 0
    msg_sent: int = 0
    tbl_ver: int = 0
    inq: int = 0
    outq: int = 0
    advertise_routes: int = 0
    advertise_routes_from: str = ""


class BgpLearnedRouteOut(BaseModel):
    vrf: str
    prefix: str
    nexthop: str
    neighbor_ip: str
    remote_as: int
    role: str
    as_path: str
    updated_at: str
    persisted: bool = True
    stale: bool = False
    data_source: str = Field(
        default="rib_sqlite",
        description="rib_sqlite=RIB 入库表；upstream_cache_sqlite=上游持久缓存合并；rib_sqlite_sync_failed=最近同步失败时表内旧行",
    )


class BgpLearnedRoutesSnapshotOut(BaseModel):
    last_sync_at: Optional[str] = None
    last_sync_ok: bool = True
    last_sync_error: str = ""
    routes: List[BgpLearnedRouteOut]
    total: int = 0
    page: int = 1
    page_size: int = 100


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
    """将 FRR 中已有邻居写入 SQLite（INSERT OR IGNORE），再套用现场角色预设（写入库）。"""
    insts = frr_bgp.list_bgp_instances()
    try:
        sd = frr_bgp.neighbor_shutdown_by_vrf_from_running_config()
    except frr_bgp.VtyshError:
        sd = {}
    for inst in insts:
        try:
            neighbors = frr_bgp.list_bgp_neighbors(inst.vrf, sd)
        except frr_bgp.VtyshError:
            continue
        for n in neighbors:
            storage.ensure_bgp_neighbor_meta_row(conn, inst.vrf, n.ip)
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


def _bgp_neighbor_out(
    conn: sqlite3.Connection,
    vrf: str,
    n: frr_bgp.BgpNeighborSummary,
    local_as: int,
) -> BgpNeighborOut:
    role, role_src = _resolve_bgp_role(conn, vrf, n.ip)
    meta = storage.get_bgp_neighbor_meta_map(conn, vrf).get(n.ip)
    note = (meta[1] if meta else "") or ""
    src_ip = (meta[2] if meta and len(meta) > 2 else "") or ""
    advertise_routes = int(meta[3]) if meta and len(meta) > 3 else 0
    advertise_routes_from = str(meta[4]) if meta and len(meta) > 4 else ""
    return BgpNeighborOut(
        vrf=vrf,
        neighbor_ip=n.ip,
        remote_as=n.remote_as,
        role=role,
        role_source=role_src,
        note=note,
        source_ip=src_ip,
        local_as=int(local_as),
        enabled=bool(n.enabled),
        session_state=n.state,
        pfx_rcd=int(n.pfx_rcd),
        up_down=n.up_down,
        neighbor_ver=int(n.neighbor_ver),
        msg_rcvd=int(n.msg_rcvd),
        msg_sent=int(n.msg_sent),
        tbl_ver=int(n.tbl_ver),
        inq=int(n.inq),
        outq=int(n.outq),
        advertise_routes=advertise_routes,
        advertise_routes_from=advertise_routes_from,
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
        storage.set_global(conn, body.hijack_enabled)
        _apply_nft(conn)
        # 实验室 ICMP TE 改写走 iptables FORWARD → te_rewrite_nfqueue，与 nft TE SNAT 并行；
        # 总开关关闭时必须清空 TE 映射并重启守护进程，否则会仍按 hop 规则替换。
        _sync_te_rewrite_best_effort(conn)
        g = storage.get_global(conn)
        return GlobalOut(hijack_enabled=g.hijack_enabled)
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
                created_at="",
            )
            for r in rows
        ]
    finally:
        conn.close()


@app.post("/api/arp-spoof/targets", response_model=ArpTargetOut)
def api_arp_spoof_targets_post(body: ArpTargetIn):
    conn = _db()
    try:
        row_id = storage.insert_arp_spoof_target(
            conn,
            spoof_gateway_ip=body.spoof_gateway_ip,
            satellite_vrf=body.satellite_vrf,
            egress_iface=body.egress_iface,
            enabled=body.enabled,
            policy_mode=body.policy_mode,
            policy_cidrs=body.policy_cidrs,
            note=body.note,
        )
        _bgp_ipvlan_reconcile_best_effort()
        _arp_reconcile_host_ip_best_effort()
        _satellite_vrf_reconcile_best_effort()
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
            created_at="",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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
        _bgp_ipvlan_reconcile_best_effort()
        _arp_reconcile_host_ip_best_effort()
        _satellite_vrf_reconcile_best_effort()
        _signal_arp_daemon_reload()
        row = storage.get_arp_spoof_target(conn, rid)
        if not row:
            raise HTTPException(status_code=404, detail="not_found")
        return ArpTargetOut(
            id=row.id,
            enabled=row.enabled,
            spoof_gateway_ip=row.spoof_gateway_ip,
            satellite_vrf=row.satellite_vrf or None,
            egress_iface=row.egress_iface,
            policy_mode=row.policy_mode,
            policy_cidrs=row.policy_cidrs,
            note=row.note,
            created_at="",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        conn.close()


@app.delete("/api/arp-spoof/targets/{rid}")
def api_arp_spoof_targets_delete(rid: int):
    conn = _db()
    try:
        if not storage.delete_arp_spoof_target(conn, rid):
            raise HTTPException(status_code=404, detail="not_found")
        _bgp_ipvlan_reconcile_best_effort()
        _arp_reconcile_host_ip_best_effort()
        _satellite_vrf_reconcile_best_effort()
        _signal_arp_daemon_reload()
        return {"ok": True}
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
        return {"arp_spoof_gateway_ips": ips}
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
    """FRR ``router bgp`` 实例，并并入内核 ``ip link type vrf`` 中尚未建仓的 VRF（``has_router_bgp=false``）。"""
    try:
        inst = frr_bgp.list_bgp_instances()
    except frr_bgp.VtyshError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    by_vrf: Dict[str, frr_bgp.BgpInstance] = {x.vrf: x for x in inst}
    out: List[BgpVrfOut] = [BgpVrfOut(vrf=x.vrf, local_as=x.local_as, has_router_bgp=True) for x in inst]
    fallback_as = frr_bgp.default_local_as_for_new_instance()
    for raw in frr_bgp.list_kernel_vrf_names():
        try:
            vn = storage.validate_vrf_name(raw)
        except ValueError:
            continue
        if vn == "default" or vn in by_vrf:
            continue
        out.append(BgpVrfOut(vrf=vn, local_as=int(fallback_as), has_router_bgp=False))
    out.sort(key=lambda x: (0 if x.vrf == "default" else 1, x.vrf, x.local_as))
    return out


@app.post("/api/bgp/instances")
def api_bgp_instances_ensure(body: BgpEnsureInstanceIn):
    """为尚无 ``router bgp`` 的 VRF 显式建仓（也可用「新增邻居」时由 ``bgp_local_as`` 自动建仓）。"""
    try:
        vrf_norm = storage.validate_vrf_name(body.vrf)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if vrf_norm == "default":
        raise HTTPException(status_code=400, detail="use_frr_manual_for_default_bgp_instance")
    las = body.local_as
    if las is None or int(las) <= 0:
        las_i = frr_bgp.default_local_as_for_new_instance()
    else:
        las_i = int(las)
        if las_i > 4294967295:
            raise HTTPException(status_code=400, detail="invalid_local_as")
    rid = (body.router_id or "").strip() or None
    try:
        _ensure_kernel_vrf_if_missing(vrf_norm, body.create_kernel_vrf_if_missing, body.kernel_rt_table)
        frr_bgp.ensure_bgp_instance(vrf_norm, las_i, router_id=rid)
    except frr_bgp.VtyshError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    inst = frr_bgp.get_instance_by_vrf(vrf_norm)
    assert inst is not None
    return {"ok": True, "vrf": vrf_norm, "local_as": inst.local_as}


@app.get("/api/bgp/neighbors", response_model=List[BgpNeighborOut])
def api_bgp_neighbors_list(vrf: Optional[str] = Query(None)):
    """
    省略 ``vrf`` 或传空：返回本机 **所有** BGP 实例（全部 VRF）的邻居合并列表；
    传入 ``vrf``（如 ``default`` / ``vrf2102``）：仅返回该 VRF。
    """
    conn = _db()
    try:
        q = (vrf or "").strip()
        if not q:
            try:
                instances = frr_bgp.list_bgp_instances()
            except frr_bgp.VtyshError as e:
                raise HTTPException(status_code=503, detail=str(e)) from e
            merged: List[BgpNeighborOut] = []
            try:
                sd = frr_bgp.neighbor_shutdown_by_vrf_from_running_config()
            except frr_bgp.VtyshError:
                sd = {}
            for inst in instances:
                vrf_norm = storage.validate_vrf_name(inst.vrf)
                try:
                    items = frr_bgp.list_bgp_neighbors(vrf_norm, sd)
                except frr_bgp.VtyshError as e:
                    raise HTTPException(status_code=503, detail=str(e)) from e
                for n in items:
                    merged.append(_bgp_neighbor_out(conn, vrf_norm, n, inst.local_as))
            return merged
        vrf_norm = storage.validate_vrf_name(q)
        inst = frr_bgp.get_instance_by_vrf(vrf_norm)
        if not inst:
            return []
        try:
            items = frr_bgp.list_bgp_neighbors(vrf_norm)
        except frr_bgp.VtyshError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        return [_bgp_neighbor_out(conn, vrf_norm, n, inst.local_as) for n in items]
    finally:
        conn.close()


@app.post("/api/bgp/sync-from-frr")
def api_bgp_sync_from_frr():
    """从 FRR 合并邻居到 meta 表，并写入预设角色（153.204 上游 / 152.204 下游，可配 MTR_BGP_DB_PRESETS）。"""
    conn = _db()
    try:
        applied = _seed_bgp_neighbors_from_frr(conn)
        return {
            "ok": True,
            "detail": "merged FRR neighbors + DB role presets",
            "presets_applied": applied,
        }
    except frr_bgp.VtyshError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    finally:
        conn.close()


@app.post("/api/bgp/neighbors", response_model=BgpNeighborOut)
def api_bgp_neighbors_add(body: BgpNeighborIn):
    conn = _db()
    try:
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
        sip = _resolve_satellite_bgp_source_ip(vrf_norm, body.source_ip)
        if (
            _satellite_style_vrf_name(vrf_norm)
            and not bgp_ipvlan_reconcile.enabled()
            and _satellite_bgp_tcp_source_mode() == "underlay"
            and not (body.source_ip or "").strip()
            and not sip
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "satellite_bgp_underlay_unknown: 未找到该卫星 VRF 的 veth 本端地址；"
                    "请先 POST /api/arp-spoof/satellite-vrfs/reconcile 或显式填写 source_ip"
                ),
            )
        if _satellite_style_vrf_name(vrf_norm) and bgp_ipvlan_reconcile.enabled():
            if not sip:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "bgp_ipvlan_source_unknown: 未找到该卫星 VRF 对应的 ARP 引流条目；"
                        "请先新增启用的 ARP 引流记录，填写 satellite_vrf，并确保 BGP VRF 与其一致"
                    ),
                )
            _bgp_ipvlan_reconcile_vrf_required(vrf_norm)
        mh = _satellite_bgp_ebgp_multihop(vrf_norm)
        if body.satellite_vrf:
            satellite_vrf_norm = storage.validate_vrf_name(body.satellite_vrf)
            try:
                vrfs = frr_bgp.list_kernel_vrf_names()
                if satellite_vrf_norm not in vrfs:
                    _ensure_kernel_vrf_if_missing(satellite_vrf_norm, True, None)
                    logger.info("created kernel vrf %s for satellite", satellite_vrf_norm)
            except Exception as e:
                logger.warning("failed to create satellite vrf %s: %s", satellite_vrf_norm, e)
        _ensure_kernel_vrf_if_missing(vrf_norm, body.create_kernel_vrf_if_missing, body.kernel_rt_table)
        inst_pre = frr_bgp.get_instance_by_vrf(vrf_norm)
        if inst_pre is None:
            if vrf_norm == "default":
                raise HTTPException(
                    status_code=400,
                    detail="bgp_instance_not_found: 请先在 FRR 中配置 router bgp <AS>（default 实例不支持自动创建）",
                )
            las_inst: Optional[int] = body.bgp_local_as
            if las_inst is None or int(las_inst) <= 0:
                las_i = frr_bgp.default_local_as_for_new_instance()
            else:
                las_i = int(las_inst)
                if las_i > 4294967295:
                    raise HTTPException(status_code=400, detail="invalid_bgp_local_as")
            rid_inst = (body.bgp_router_id or "").strip() or (sip if _satellite_style_vrf_name(vrf_norm) else "") or None
            try:
                frr_bgp.ensure_bgp_instance(vrf_norm, las_i, router_id=rid_inst)
            except frr_bgp.VtyshError as e:
                raise HTTPException(status_code=503, detail=str(e)) from e
        try:
            lst0 = frr_bgp.list_bgp_neighbors(vrf_norm)
        except frr_bgp.VtyshError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        dup = next((x for x in lst0 if x.ip == ip), None)
        if dup is not None:
            hint_src = ""
            if sip:
                hint_src = " 若仅需更换本机 TCP 源（update-source），请在列表中对已有邻居点「编辑」修改，勿重复新增。"
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "neighbor_already_exists",
                    "vrf": vrf_norm,
                    "neighbor_ip": ip,
                    "message": (
                        f"FRR 在 VRF {vrf_norm} 下已存在邻居 {ip}（每个对端 IPv4 仅能一条 BGP 会话）。"
                        "要以另一本机源地址再连同一台物理对端，需在对端增加第二个用于 BGP 的可达地址（如 loopback），"
                        "在此填写新的「邻居 IP」。"
                        + hint_src
                    ),
                },
            )
        frr_neighbor_added = False
        try:
            if sip:
                storage.validate_ipv4(sip)
            
            inst = frr_bgp.get_instance_by_vrf(vrf_norm)
            if not inst:
                local_as = int(os.environ.get("MTR_BGP_ENSURE_LOCAL_AS", "63199"))
                frr_bgp.ensure_bgp_instance(vrf_norm, local_as, router_id=sip)
                logger.info(f"Created BGP instance for VRF {vrf_norm} with router-id {sip}")
            
            frr_bgp.add_neighbor_ipv4_unicast(
                vrf_norm,
                ip,
                int(body.remote_as),
                update_source=sip or None,
                ebgp_multihop=mh,
            )
            frr_neighbor_added = True
        except frr_bgp.VtyshError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        try:
            storage.set_bgp_neighbor_meta(conn, vrf_norm, ip, role, "", update_source=sip if sip else None)
        except Exception as e:
            if frr_neighbor_added:
                try:
                    frr_bgp.remove_neighbor_ipv4(vrf_norm, ip)
                except frr_bgp.VtyshError:
                    logger.exception("rollback FRR neighbor %s vrf %s failed", ip, vrf_norm)
            logger.exception("bgp_neighbor_meta write failed vrf=%s ip=%s", vrf_norm, ip)
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "bgp_meta_write_failed",
                    "message": f"FRR 已配置邻居 {ip}，但元数据写入失败：{e}。请刷新页面后编辑或删除重复邻居。",
                },
            ) from e
        if sip:
            if _satellite_style_vrf_name(vrf_norm) and bgp_ipvlan_reconcile.enabled():
                _bgp_ipvlan_reconcile_vrf_best_effort(vrf_norm)
            _arp_reconcile_host_ip_best_effort()
        inst = frr_bgp.get_instance_by_vrf(vrf_norm)
        local_as = int(inst.local_as) if inst else 0
        try:
            lst = frr_bgp.list_bgp_neighbors(vrf_norm)
        except frr_bgp.VtyshError:
            lst = []
        found = next((x for x in lst if x.ip == ip), None)
        if not found:
            role2, rs = _resolve_bgp_role(conn, vrf_norm, ip)
            meta_row = storage.get_bgp_neighbor_meta_map(conn, vrf_norm).get(ip)
            sip_out = (meta_row[2] if meta_row and len(meta_row) > 2 else "") or ""
            return BgpNeighborOut(
                vrf=vrf_norm,
                neighbor_ip=ip,
                remote_as=int(body.remote_as),
                role=role2,
                role_source=rs,
                note="",
                source_ip=sip_out,
                local_as=local_as,
                enabled=True,
                session_state="Unknown",
                pfx_rcd=0,
                up_down="—",
            )
        return _bgp_neighbor_out(conn, vrf_norm, found, local_as)
    finally:
        conn.close()


@app.patch("/api/bgp/neighbors/{vrf}/{neighbor_ip}", response_model=BgpNeighborOut)
def api_bgp_neighbors_patch(vrf: str, neighbor_ip: str, body: BgpNeighborPatch):
    conn = _db()
    try:
        vrf_norm = storage.validate_vrf_name(vrf)
        ip = storage.validate_ipv4(neighbor_ip)
        rename_to: Optional[str] = None
        if body.neighbor_ip is not None:
            cand = (body.neighbor_ip or "").strip()
            if cand:
                rename_to = storage.validate_ipv4(cand)
                if rename_to == ip:
                    rename_to = None
        if (
            body.remote_as is None
            and body.role is None
            and body.note is None
            and body.source_ip is None
            and rename_to is None
        ):
            raise HTTPException(status_code=400, detail="empty_patch")
        inst = frr_bgp.get_instance_by_vrf(vrf_norm)
        if not inst:
            raise HTTPException(status_code=404, detail="vrf_not_found")
        try:
            lst = frr_bgp.list_bgp_neighbors(vrf_norm)
        except frr_bgp.VtyshError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        cur = next((x for x in lst if x.ip == ip), None)
        if not cur:
            raise HTTPException(status_code=404, detail="neighbor_not_found")

        meta_row = storage.get_bgp_neighbor_meta_map(conn, vrf_norm).get(ip)
        cur_role = (meta_row[0] if meta_row else "unknown") or "unknown"
        cur_note = (meta_row[1] if meta_row else "") or ""
        cur_src = (meta_row[2] if meta_row and len(meta_row) > 2 else "") or ""

        if rename_to is not None:
            if any(x.ip == rename_to for x in lst):
                raise HTTPException(status_code=409, detail="neighbor_ip_conflict")
            merged_ras = int(body.remote_as) if body.remote_as is not None else int(cur.remote_as)
            if merged_ras <= 0 or merged_ras > 4294967295:
                raise HTTPException(status_code=400, detail="invalid_remote_as")
            merged_note = body.note if body.note is not None else cur_note
            merged_role = cur_role
            if body.role is not None:
                merged_role = body.role.strip().lower()
                if merged_role not in storage.BGP_META_ROLES:
                    raise HTTPException(status_code=400, detail="invalid_role")
            merged_src = (body.source_ip or "").strip() if body.source_ip is not None else (cur_src or "").strip()
            if _satellite_style_vrf_name(vrf_norm) and bgp_ipvlan_reconcile.enabled():
                auto_src = bgp_ipvlan_reconcile.source_ip_for_vrf(DB_PATH, vrf_norm) or ""
                if not merged_src and auto_src:
                    merged_src = auto_src
                if merged_src:
                    _bgp_ipvlan_reconcile_vrf_best_effort(vrf_norm)
            if merged_src:
                try:
                    storage.validate_ipv4(merged_src)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail="invalid_source_ip") from e
            try:
                frr_bgp.rename_neighbor_ipv4(
                    vrf_norm,
                    ip,
                    rename_to,
                    merged_ras,
                    merged_src or None,
                    bool(cur.enabled),
                    ebgp_multihop=_satellite_bgp_ebgp_multihop(vrf_norm),
                )
            except frr_bgp.VtyshError as e:
                raise HTTPException(status_code=503, detail=str(e)) from e
            storage.delete_bgp_neighbor_meta(conn, vrf_norm, ip)
            storage.set_bgp_neighbor_meta(
                conn,
                vrf_norm,
                rename_to,
                merged_role,
                merged_note,
                update_source=merged_src,
            )
            if _satellite_style_vrf_name(vrf_norm) and bgp_ipvlan_reconcile.enabled():
                _bgp_ipvlan_reconcile_vrf_best_effort(vrf_norm)
            _arp_reconcile_host_ip_best_effort()
            lst2 = frr_bgp.list_bgp_neighbors(vrf_norm)
            found = next((x for x in lst2 if x.ip == rename_to), None)
            if not found:
                meta2 = storage.get_bgp_neighbor_meta_map(conn, vrf_norm).get(rename_to)
                sip2 = (meta2[2] if meta2 and len(meta2) > 2 else "") or ""
                role2, rs = _resolve_bgp_role(conn, vrf_norm, rename_to)
                return BgpNeighborOut(
                    vrf=vrf_norm,
                    neighbor_ip=rename_to,
                    remote_as=merged_ras,
                    role=role2,
                    role_source=rs,
                    note=merged_note,
                    source_ip=sip2,
                    local_as=int(inst.local_as),
                    enabled=True,
                    session_state="Unknown",
                    pfx_rcd=0,
                    up_down="—",
                )
            return _bgp_neighbor_out(conn, vrf_norm, found, inst.local_as)

        new_src = cur_src
        if body.source_ip is not None:
            new_src = (body.source_ip or "").strip()
            if new_src:
                try:
                    storage.validate_ipv4(new_src)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail="invalid_source_ip") from e
        if _satellite_style_vrf_name(vrf_norm) and bgp_ipvlan_reconcile.enabled():
            auto_src = bgp_ipvlan_reconcile.source_ip_for_vrf(DB_PATH, vrf_norm) or ""
            if not new_src and auto_src:
                new_src = auto_src
            if new_src:
                _bgp_ipvlan_reconcile_vrf_best_effort(vrf_norm)

        if body.remote_as is not None:
            ras = int(body.remote_as)
            if ras <= 0 or ras > 4294967295:
                raise HTTPException(status_code=400, detail="invalid_remote_as")
            if ras != cur.remote_as:
                was_shut = not cur.enabled
                try:
                    frr_bgp.replace_neighbor_remote_as(
                        vrf_norm,
                        ip,
                        ras,
                        was_shut,
                        update_source=new_src or None,
                        ebgp_multihop=_satellite_bgp_ebgp_multihop(vrf_norm),
                    )
                except frr_bgp.VtyshError as e:
                    raise HTTPException(status_code=503, detail=str(e)) from e
            elif body.source_ip is not None:
                try:
                    frr_bgp.set_neighbor_update_source(vrf_norm, ip, new_src or None)
                    _ensure_satellite_ebgp_multihop(vrf_norm, ip)
                except frr_bgp.VtyshError as e:
                    raise HTTPException(status_code=503, detail=str(e)) from e
        elif body.source_ip is not None:
            try:
                frr_bgp.set_neighbor_update_source(vrf_norm, ip, new_src or None)
                _ensure_satellite_ebgp_multihop(vrf_norm, ip)
            except frr_bgp.VtyshError as e:
                raise HTTPException(status_code=503, detail=str(e)) from e

        new_role = cur_role
        if body.role is not None:
            r = body.role.strip().lower()
            if r not in storage.BGP_META_ROLES:
                raise HTTPException(status_code=400, detail="invalid_role")
            new_role = r
        new_note = body.note if body.note is not None else cur_note
        if body.role is not None or body.note is not None or body.source_ip is not None:
            us_arg: Optional[str] = None
            if body.source_ip is not None:
                us_arg = (body.source_ip or "").strip()
                if _satellite_style_vrf_name(vrf_norm) and bgp_ipvlan_reconcile.enabled() and not us_arg:
                    us_arg = bgp_ipvlan_reconcile.source_ip_for_vrf(DB_PATH, vrf_norm) or ""
            storage.set_bgp_neighbor_meta(conn, vrf_norm, ip, new_role, new_note, update_source=us_arg)

        if body.source_ip is not None:
            if _satellite_style_vrf_name(vrf_norm) and bgp_ipvlan_reconcile.enabled():
                _bgp_ipvlan_reconcile_vrf_best_effort(vrf_norm)
            _arp_reconcile_host_ip_best_effort()

        lst2 = frr_bgp.list_bgp_neighbors(vrf_norm)
        found = next((x for x in lst2 if x.ip == ip), None)
        if not found:
            role2, rs = _resolve_bgp_role(conn, vrf_norm, ip)
            meta2 = storage.get_bgp_neighbor_meta_map(conn, vrf_norm).get(ip)
            sip2 = (meta2[2] if meta2 and len(meta2) > 2 else "") or ""
            return BgpNeighborOut(
                vrf=vrf_norm,
                neighbor_ip=ip,
                remote_as=int(body.remote_as) if body.remote_as is not None else cur.remote_as,
                role=role2,
                role_source=rs,
                note=new_note,
                source_ip=sip2,
                local_as=int(inst.local_as),
                enabled=True,
                session_state="Unknown",
                pfx_rcd=0,
                up_down="—",
            )
        return _bgp_neighbor_out(conn, vrf_norm, found, inst.local_as)
    finally:
        conn.close()


@app.delete("/api/bgp/neighbors/{vrf}/{neighbor_ip}")
def api_bgp_neighbors_delete(vrf: str, neighbor_ip: str):
    conn = _db()
    try:
        vrf_norm = storage.validate_vrf_name(vrf)
        ip = storage.validate_ipv4(neighbor_ip)
        if not frr_bgp.get_instance_by_vrf(vrf_norm):
            raise HTTPException(status_code=404, detail="vrf_not_found")
        try:
            lst = frr_bgp.list_bgp_neighbors(vrf_norm)
        except frr_bgp.VtyshError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        if not any(x.ip == ip for x in lst):
            raise HTTPException(status_code=404, detail="neighbor_not_found")
        try:
            frr_bgp.remove_neighbor_ipv4(vrf_norm, ip)
        except frr_bgp.VtyshError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        storage.delete_bgp_neighbor_meta(conn, vrf_norm, ip)
        deleted_routes = storage.delete_bgp_learned_routes_by_neighbor_ip(conn, ip)
        return {"ok": True, "deleted_routes": deleted_routes}
    finally:
        conn.close()


@app.post("/api/bgp/neighbors/{vrf}/{neighbor_ip}/toggle", response_model=BgpNeighborOut)
def api_bgp_neighbors_toggle(vrf: str, neighbor_ip: str, body: BgpNeighborToggleIn):
    conn = _db()
    try:
        vrf_norm = storage.validate_vrf_name(vrf)
        ip = storage.validate_ipv4(neighbor_ip)
        inst = frr_bgp.get_instance_by_vrf(vrf_norm)
        if not inst:
            raise HTTPException(status_code=404, detail="vrf_not_found")
        try:
            frr_bgp.set_neighbor_enabled(vrf_norm, ip, bool(body.enabled))
        except frr_bgp.VtyshError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        lst = frr_bgp.list_bgp_neighbors(vrf_norm)
        found = next((x for x in lst if x.ip == ip), None)
        if not found:
            role2, rs = _resolve_bgp_role(conn, vrf_norm, ip)
            meta_t = storage.get_bgp_neighbor_meta_map(conn, vrf_norm).get(ip)
            note_t = (meta_t[1] if meta_t else "") or ""
            src_t = (meta_t[2] if meta_t and len(meta_t) > 2 else "") or ""
            return BgpNeighborOut(
                vrf=vrf_norm,
                neighbor_ip=ip,
                remote_as=0,
                role=role2,
                role_source=rs,
                note=note_t,
                source_ip=src_t,
                local_as=int(inst.local_as),
                enabled=bool(body.enabled),
                session_state="Unknown",
                pfx_rcd=0,
                up_down="—",
            )
        out = _bgp_neighbor_out(conn, vrf_norm, found, inst.local_as)
        if out.enabled != bool(body.enabled):
            out = out.model_copy(update={"enabled": bool(body.enabled)})
        return out
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
    设置邻居的路由通告功能。
    - advertise_routes: 0-禁用，1-启用
    - advertise_routes_from: 从哪个邻居IP学习路由并通告给本邻居
    """
    conn = _db()
    try:
        vrf_norm = storage.validate_vrf_name(vrf)
        ip = storage.validate_ipv4(neighbor_ip)

        storage.update_bgp_neighbor_advertise_routes(
            conn, vrf_norm, ip, body.advertise_routes, body.advertise_routes_from
        )

        if body.advertise_routes:
            # 启动异步路由通告任务
            # 当advertise_routes_from为空时，会自动从数据库查找路由来源
            task_id = f"{vrf_norm}-{ip}-advertise"
            async with _ADVERTISE_LOCK:
                if task_id in _ADVERTISE_TASKS:
                    if _ADVERTISE_TASKS[task_id]["status"] == "running":
                        logger.info(f"Advertise task already running: {task_id}")
                    else:
                        _ADVERTISE_TASKS.pop(task_id, None)

                _ADVERTISE_TASKS[task_id] = {
                    "status": "running",
                    "progress": 0,
                    "total_routes": 0,
                    "added": 0,
                    "message": "Starting... (auto-discovering route sources)"
                }

            asyncio.create_task(_async_apply_bgp_route_advertise(vrf_norm, ip, body.advertise_routes_from or "", task_id))
        else:
            # 禁用路由通告
            task_id = f"{vrf_norm}-{ip}-advertise"
            async with _ADVERTISE_LOCK:
                if task_id in _ADVERTISE_TASKS:
                    _ADVERTISE_TASKS.pop(task_id, None)
            logger.info(f"Advertise task disabled: {task_id}")

        inst = frr_bgp.get_instance_by_vrf(vrf_norm)
        if inst:
            lst = frr_bgp.list_bgp_neighbors(vrf_norm)
            found = next((x for x in lst if x.ip == ip), None)
            if found:
                return _bgp_neighbor_out(conn, vrf_norm, found, inst.local_as)

        role2, rs = _resolve_bgp_role(conn, vrf_norm, ip)
        meta_t = storage.get_bgp_neighbor_meta_map(conn, vrf_norm).get(ip)
        note_t = (meta_t[1] if meta_t else "") or ""
        src_t = (meta_t[2] if meta_t and len(meta_t) > 2 else "") or ""
        ar_t = int(meta_t[3]) if meta_t and len(meta_t) > 3 else 0
        arf_t = str(meta_t[4]) if meta_t and len(meta_t) > 4 else ""

        return BgpNeighborOut(
            vrf=vrf_norm,
            neighbor_ip=ip,
            remote_as=0,
            role=role2,
            role_source=rs,
            note=note_t,
            source_ip=src_t,
            local_as=int(inst.local_as) if inst else 0,
            enabled=True,
            session_state="Unknown",
            pfx_rcd=0,
            up_down="—",
            advertise_routes=ar_t,
            advertise_routes_from=arf_t,
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


async def _async_apply_bgp_route_advertise(vrf: str, target_neighbor: str, source_neighbor_ip: str, task_id: str):
    """异步执行路由通告
    
    将从所有其他邻居学到的路由通告给目标邻居。
    """
    try:
        conn = _db()
        try:
            # 查找所有其他邻居（除了目标邻居自己）
            source_neighbors = storage.get_all_other_neighbors(conn, target_neighbor)
            
            if not source_neighbors:
                async with _ADVERTISE_LOCK:
                    if task_id in _ADVERTISE_TASKS:
                        _ADVERTISE_TASKS[task_id]["status"] = "completed"
                        _ADVERTISE_TASKS[task_id]["message"] = f"No other neighbors found"
                return
            
            logger.info(f"Advertise task {task_id}: found {len(source_neighbors)} other neighbors: {source_neighbors}")
            
            # 统计所有来源的总路由数
            total_routes_count = 0
            for src_ip in source_neighbors:
                count = storage.count_bgp_learned_routes_by_neighbor_ip(conn, src_ip)
                total_routes_count += count
            
            async with _ADVERTISE_LOCK:
                if task_id in _ADVERTISE_TASKS:
                    _ADVERTISE_TASKS[task_id]["total_routes"] = total_routes_count
                    _ADVERTISE_TASKS[task_id]["message"] = f"Found {total_routes_count} routes from {len(source_neighbors)} neighbors"
            
            if total_routes_count == 0:
                async with _ADVERTISE_LOCK:
                    if task_id in _ADVERTISE_TASKS:
                        _ADVERTISE_TASKS[task_id]["status"] = "completed"
                        _ADVERTISE_TASKS[task_id]["message"] = f"No routes to advertise"
                return
            
            # 批量处理所有来源邻居的路由
            batch_size = 10000
            routes_batch = []
            total_added = 0
            processed = 0
            
            for src_ip in source_neighbors:
                for prefix, nexthop in storage.iter_bgp_learned_routes_by_neighbor_ip(conn, src_ip, batch_size):
                    routes_batch.append((prefix, nexthop))
                    
                    if len(routes_batch) >= batch_size:
                        result = await _run_blocking_call(frr_bgp.add_bgp_networks_batch, vrf, routes_batch)
                        total_added += result.get("added", 0)
                        processed += len(routes_batch)
                        routes_batch = []
                        
                        async with _ADVERTISE_LOCK:
                            if task_id in _ADVERTISE_TASKS:
                                _ADVERTISE_TASKS[task_id]["progress"] = int((processed / total_routes_count) * 100)
                                _ADVERTISE_TASKS[task_id]["added"] = total_added
                                _ADVERTISE_TASKS[task_id]["message"] = f"Processed {processed}/{total_routes_count} routes"
            
            # 处理剩余路由
            if routes_batch:
                result = await _run_blocking_call(frr_bgp.add_bgp_networks_batch, vrf, routes_batch)
                total_added += result.get("added", 0)
            
            async with _ADVERTISE_LOCK:
                if task_id in _ADVERTISE_TASKS:
                    _ADVERTISE_TASKS[task_id]["status"] = "completed"
                    _ADVERTISE_TASKS[task_id]["progress"] = 100
                    _ADVERTISE_TASKS[task_id]["added"] = total_added
                    _ADVERTISE_TASKS[task_id]["message"] = f"Completed: {total_added}/{total_routes_count} routes from {len(source_neighbors)} neighbors"
            
            logger.info(f"Advertise task {task_id} completed: {total_added}/{total_routes_count} routes from {len(source_neighbors)} neighbors")
        finally:
            conn.close()
    except Exception as e:
        async with _ADVERTISE_LOCK:
            if task_id in _ADVERTISE_TASKS:
                _ADVERTISE_TASKS[task_id]["status"] = "error"
                _ADVERTISE_TASKS[task_id]["message"] = str(e)[:200]
        logger.error(f"Advertise task {task_id} failed: {str(e)}")


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
                    result = frr_bgp.add_bgp_networks_batch(vrf, routes_batch)
                    total_added += result.get("added", 0)
                    routes_batch = []

            if routes_batch:
                result = frr_bgp.add_bgp_networks_batch(vrf, routes_batch)
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
def api_bgp_learned_routes_filter_options():
    """下拉用：库中曾出现过的 VRF、来源邻居 IP（含上游缓存表）。"""
    conn = _db()
    try:
        return {
            "vrfs": storage.list_bgp_distinct_learned_vrfs(conn),
            "neighbor_ips": storage.list_bgp_distinct_learned_neighbor_ips(conn),
        }
    finally:
        conn.close()


@app.get("/api/bgp/learned-routes", response_model=BgpLearnedRoutesSnapshotOut)
def api_bgp_learned_routes_list(
    vrf: Optional[str] = Query(None, description="按 VRF 筛选；省略表示全部"),
    neighbor_ip: Optional[str] = Query(None, description="按来源邻居 IP 精确筛选；省略表示全部"),
    merge_upstream_stale: bool = Query(True, description="合并上游持久缓存中、当前 RIB 快照已缺失的前缀（stale=true）"),
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(100, ge=1, le=1000, description="每页条数，范围 1-1000"),
):
    """从 SQLite 读取最近一次同步的 BGP 学习路由快照；可选合并上游断连后仍保留的缓存前缀。"""
    conn = _db()
    try:
        st = storage.get_bgp_rib_sync_state(conn)
        sync_ok = bool(st[1])
        q = (vrf or "").strip() or None
        nip_raw = (neighbor_ip or "").strip()
        nip: Optional[str] = None
        if nip_raw:
            try:
                nip = storage.validate_ipv4(nip_raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid neighbor_ip")
        total = storage.count_bgp_learned_routes(conn, q, nip)
        rows = storage.list_bgp_learned_routes(conn, q, nip, page, page_size)
        rib_src = "rib_sqlite" if sync_ok else "rib_sqlite_sync_failed"
        routes = [
            BgpLearnedRouteOut(
                vrf=str(r["vrf"]),
                prefix=str(r["prefix"]),
                nexthop=str(r["nexthop"] or ""),
                neighbor_ip=str(r["neighbor_ip"] or ""),
                remote_as=int(r["remote_as"] or 0),
                role=str(r["role"] or "unknown"),
                as_path=str(r["as_path"] or ""),
                updated_at=str(r["updated_at"] or ""),
                persisted=True,
                stale=False,
                data_source=rib_src,
            )
            for r in rows
        ]
        if merge_upstream_stale:
            learn_vrf = bgp_sticky_reconcile.upstream_cache_learn_vrf()
            if not q or q == learn_vrf:
                live_u = {str(r["prefix"]) for r in storage.list_bgp_learned_routes(conn, learn_vrf)}
                for d in bgp_sticky_reconcile.merge_stale_upstream_into_routes(conn, learn_vrf, live_u):
                    if nip and str(d.get("neighbor_ip") or "").strip() != nip:
                        continue
                    routes.append(
                        BgpLearnedRouteOut(
                            vrf=str(d["vrf"]),
                            prefix=str(d["prefix"]),
                            nexthop=str(d["nexthop"] or ""),
                            neighbor_ip=str(d["neighbor_ip"] or ""),
                            remote_as=int(d["remote_as"] or 0),
                            role=str(d["role"] or "upstream"),
                            as_path=str(d["as_path"] or ""),
                            updated_at=str(d["updated_at"] or ""),
                            persisted=True,
                            stale=bool(d.get("stale")),
                            data_source="upstream_cache_sqlite",
                        )
                    )
        return BgpLearnedRoutesSnapshotOut(
            last_sync_at=st[0],
            last_sync_ok=bool(st[1]),
            last_sync_error=str(st[2] or ""),
            routes=routes,
            total=total,
            page=page,
            page_size=page_size,
        )
    finally:
        conn.close()


@app.post("/api/bgp/learned-routes/sync")
async def api_bgp_learned_routes_sync_now():
    """立即从 FRR 拉取 RIB 并刷新本地表；若启用 sticky，返回下游通告协调摘要。"""
    sticky = await _run_blocking_call(bgp_learned_routes_sync.sync_bgp_learned_routes, DB_PATH)
    return {"ok": True, "sticky": sticky or {}}


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
