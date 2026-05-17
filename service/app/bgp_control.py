"""
BGP 控制面：统一通过 GoBGP Agent（RX/TX），不再使用 FRR vtysh 管理邻居。

RR 会话由 OP「BGP 管理」创建（写入 SQLite meta + POST /api/rr/config），
部署脚本仅安装 bgp-agent（local-as / router-id），不在 systemd 中硬编码 -rr。
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from . import storage

logger = logging.getLogger(__name__)

GOBGP_VRF_RR = "gobgp-rr"
BGP_AGENT_ENV_FILE = Path(
    os.environ.get("MTR_BGP_AGENT_ENV_FILE", "/var/lib/bgp_agent/bgp-agent.env")
)


def agent_url() -> str:
    return (os.environ.get("GOBGP_AGENT_URL") or "http://127.0.0.1:9179").rstrip("/")


def default_local_as() -> int:
    return int(os.environ.get("LOCAL_AS", "63199"))


def default_router_id() -> str:
    """与 RR 建连的本端地址（BGP Router ID / update-source），非 OP SSH 管理 IP。"""
    return os.environ.get("ROUTER_ID", "139.159.43.207").strip()


def is_rr_role(role: str) -> bool:
    return (role or "").strip().lower() in {"rr", "upstream"}


def is_downstream_role(role: str) -> bool:
    return (role or "").strip().lower() == "downstream"


def _client() -> httpx.Client:
    return httpx.Client(base_url=agent_url(), timeout=30.0)


def health_ok() -> bool:
    try:
        with _client() as c:
            r = c.get("/health")
            return r.status_code == 200 and (r.json() or {}).get("status") == "ok"
    except Exception:
        return False


def require_agent() -> None:
    if not health_ok():
        raise RuntimeError("GoBGP Agent 不可用，请检查 bgp-agent.service")


def list_agent_neighbors() -> List[Dict[str, Any]]:
    require_agent()
    with _client() as c:
        r = c.get("/api/neighbors")
        r.raise_for_status()
        return list((r.json() or {}).get("neighbors") or [])


def add_neighbor(
    vrf: str,
    address: str,
    remote_as: int,
    role: str,
    local_address: str = "",
    ebgp_multihop: int = 0,
    bind_interface: str = "",
    passive_mode: bool = False,
) -> Dict[str, Any]:
    require_agent()
    body: Dict[str, Any] = {
        "address": address,
        "remote_as": int(remote_as),
        "role": role,
        "vrf": vrf,
        "local_address": (local_address or "").strip(),
        "ebgp_multihop": int(ebgp_multihop or 0),
        "bind_interface": (bind_interface or "").strip(),
        "passive_mode": bool(passive_mode),
    }
    with _client() as c:
        r = c.post("/api/neighbors/add", json=body)
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
        return r.json()


def remove_neighbor(vrf: str, address: str) -> None:
    require_agent()
    with _client() as c:
        r = c.post("/api/neighbors/remove", json={"address": address, "vrf": vrf})
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")


def set_neighbor_enabled(vrf: str, address: str, enabled: bool) -> None:
    require_agent()
    with _client() as c:
        r = c.post(
            "/api/neighbors/toggle",
            json={"address": address, "vrf": vrf, "enabled": bool(enabled)},
        )
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")


def _persist_bgp_agent_env(
    *,
    rr_addr: Optional[str] = None,
    rr_as: Optional[int] = None,
    router_id: Optional[str] = None,
) -> None:
    """记录 OP 下发的 RR，供运维查看；Agent 运行态以 API 为准。"""
    try:
        env = _agent_env()
        if rr_addr is not None:
            env["rr_addr"] = rr_addr.strip()
        if rr_as is not None:
            env["rr_as"] = str(int(rr_as))
        if router_id is not None and router_id.strip():
            env["router_id"] = router_id.strip()
        BGP_AGENT_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"LOCAL_AS={env['local_as']}",
            f"ROUTER_ID={env['router_id']}",
            f"RR_ADDR={env.get('rr_addr', '')}",
            f"RR_AS={env.get('rr_as', '')}",
        ]
        BGP_AGENT_ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning("persist bgp-agent.env: %s", e)


def configure_rr(address: str, remote_as: int, local_address: str = "") -> Dict[str, Any]:
    """由 OP 创建/更新 RR（RX 单会话）；local_address 为与 RR 直连的本端 IP。"""
    require_agent()
    addr = (address or "").strip()
    if not addr:
        raise ValueError("rr address required")
    ras = int(remote_as) if int(remote_as) > 0 else default_local_as()
    la = (local_address or "").strip() or default_router_id()
    with _client() as c:
        r = c.post(
            "/api/rr/config",
            json={"address": addr, "remote_as": ras, "local_address": la},
        )
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
        out = r.json() if r.content else {}
    _persist_bgp_agent_env(rr_addr=addr, rr_as=ras, router_id=la)
    return {
        "ok": True,
        "restarted": False,
        "rr_addr": addr,
        "rr_as": ras,
        "local_address": la,
        **(out or {}),
    }


def remove_rr() -> None:
    require_agent()
    with _client() as c:
        r = c.post("/api/rr/remove", json={})
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
    _persist_bgp_agent_env(rr_addr="", rr_as=0)


# 兼容旧调用名
ensure_rr = configure_rr


def get_status() -> Dict[str, Any]:
    try:
        with _client() as c:
            r = c.get("/api/status")
            r.raise_for_status()
            return r.json()
    except Exception:
        return {}


def agent_env_config() -> Dict[str, str]:
    """Agent / 本机 BGP 环境（RR 以 meta / OP 为准，RR_ADDR 仅作表单提示默认值）。"""
    return _agent_env()


def _agent_env() -> Dict[str, str]:
    rr_addr = os.environ.get("RR_ADDR", "").strip()
    rr_as = os.environ.get("RR_AS", "").strip()
    if BGP_AGENT_ENV_FILE.is_file():
        try:
            for line in BGP_AGENT_ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k == "RR_ADDR" and v:
                    rr_addr = v
                elif k == "RR_AS" and v:
                    rr_as = v
        except OSError:
            pass
    if not rr_as:
        rr_as = os.environ.get("LOCAL_AS", "63199").strip()
    return {
        "rr_addr": rr_addr,
        "rr_as": rr_as,
        "local_as": os.environ.get("LOCAL_AS", "63199").strip(),
        "router_id": default_router_id(),
        "redis_addr": os.environ.get("REDIS_ADDR", "localhost:6379").strip(),
        "rocksdb_path": os.environ.get("ROCKSDB_PATH", "/var/lib/bgp_agent/rocksdb").strip(),
        "api_addr": os.environ.get("API_ADDR", ":9179").strip(),
        "remote_dir": os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").strip(),
    }


def _systemd_sync_script(cfg: Dict[str, str], rebuild: bool = False) -> str:
    """部署用：仅 local-as / router-id，RR 由 OP 创建。"""
    op_dir = cfg["remote_dir"]
    rocks = cfg["rocksdb_path"]
    rebuild_block = ""
    if rebuild:
        rebuild_block = f"""
export PATH=/usr/local/go/bin:$PATH
export GOPROXY=https://goproxy.cn,direct
export CGO_ENABLED=1
cd {op_dir}/bgp_agent && go build -o bgp_agent -ldflags="-s -w" .
"""
    return f"""
set -e
{rebuild_block}
mkdir -p {rocks}
mkdir -p $(dirname {BGP_AGENT_ENV_FILE})
cat > /etc/systemd/system/bgp-agent.service <<'UNIT'
[Unit]
Description=BGP RX/TX Agent (GoBGP)
After=network.target redis-server.service
Wants=redis-server.service
[Service]
Type=simple
WorkingDirectory={op_dir}/bgp_agent
Environment=PATH=/usr/local/go/bin:/usr/bin:/bin
ExecStart={op_dir}/bgp_agent/bgp_agent \\
  -local-as {cfg["local_as"]} -router-id {cfg["router_id"]} \\
  -redis {cfg["redis_addr"]} -rocksdb {cfg["rocksdb_path"]} \\
  -api {cfg["api_addr"]}
Restart=always
RestartSec=10
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl restart bgp-agent
sleep 3
curl -sf http://127.0.0.1:9179/health
"""


def sync_bgp_agent_systemd(rebuild: bool = False) -> None:
    """仅更新 bgp-agent 单元（不含 RR 邻居参数）。"""
    cfg = _agent_env()
    script = _systemd_sync_script(cfg, rebuild=rebuild)
    proc = subprocess.run(
        ["bash", "-se"],
        input=script,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "bgp-agent sync failed")[:500])


def find_rr_meta(conn) -> Optional[Tuple[str, str, str]]:
    """返回 (vrf, neighbor_ip, source_ip) 或 None。"""
    for vrf, nip, role, _note, src in _iter_meta(conn):
        if is_rr_role(role):
            return vrf, nip, src
    return None


def clear_rr_meta_except(conn, keep_vrf: str, keep_ip: str) -> None:
    """全局仅保留一条 RR meta（单会话收路由）。"""
    for vrf, nip, role, _note, _src in _iter_meta(conn):
        if is_rr_role(role) and not (vrf == keep_vrf and nip == keep_ip):
            storage.delete_bgp_neighbor_meta(conn, vrf, nip)


def reconcile_meta_to_agent(conn) -> Dict[str, Any]:
    """按 SQLite meta 在 Agent 上恢复邻居（RR 与下游均来自 OP 录入）。"""
    summary: Dict[str, Any] = {"added": [], "errors": []}
    if not health_ok():
        summary["skipped"] = "agent_down"
        return summary
    rr_done = False
    rr_meta = find_rr_meta(conn)
    for vrf, nip, role, _note, src in _iter_meta(conn):
        r = (role or "").strip().lower()
        try:
            if is_rr_role(r):
                if rr_done:
                    continue
                ras = int(_agent_env().get("rr_as") or 0) or default_local_as()
                try:
                    row = next(
                        (x for x in list_agent_neighbors() if str(x.get("address")) == nip),
                        None,
                    )
                    if row:
                        ras = int(row.get("remote_as") or ras)
                except Exception:
                    pass
                configure_rr(nip, ras, local_address=(src or "").strip() or default_router_id())
                summary["added"].append(f"rr:{nip}")
                rr_done = True
            elif is_downstream_role(r):
                env = _agent_env()
                dras = int(os.environ.get("MTR_DOWNSTREAM_REMOTE_AS", env["local_as"]))
                add_neighbor(vrf, nip, dras, "downstream", local_address=src or "")
                summary["added"].append(f"{vrf}:{nip}")
        except Exception as e:
            summary["errors"].append(f"{vrf}:{nip}:{e}")
    return summary


def _iter_meta(conn) -> List[Tuple[str, str, str, str, str]]:
    out: List[Tuple[str, str, str, str, str]] = []
    for row in conn.execute(
        "SELECT vrf, neighbor_ip, role, note, source_ip FROM bgp_neighbor_meta ORDER BY vrf, neighbor_ip"
    ):
        out.append((str(row[0]), str(row[1]), str(row[2]), str(row[3] or ""), str(row[4] or "")))
    return out


def list_vrfs_from_meta(conn) -> List[str]:
    vrfs = set()
    for vrf, _nip, _r, _n, _s in _iter_meta(conn):
        vrfs.add(vrf)
    for name in storage.list_satellite_vrf_names(conn):
        vrfs.add(name)
    return sorted(vrfs)


def production_form_hints() -> Dict[str, Any]:
    env = _agent_env()
    rr_ip = env["rr_addr"] or os.environ.get("MTR_FORM_RR_HINT", "139.159.43.249").strip()
    return {
        "rr_neighbor_ip": rr_ip,
        "downstream_neighbor_ip": os.environ.get("MTR_SATELLITE_PEER_IP", "").strip(),
        "local_as": int(env["local_as"]),
        "router_id": env["router_id"],
        "architecture": "gobgp_rx_tx",
        "rr_from_op": True,
    }


def agent_row_to_state_label(state: str) -> str:
    s = (state or "").strip()
    if "ESTABLISHED" in s.upper():
        return "Established"
    if "ACTIVE" in s.upper():
        return "Active"
    return s or "Unknown"


def rr_is_established() -> bool:
    st = get_status()
    proc = st.get("processor") or {}
    if proc.get("rr_connected"):
        return True
    rx = st.get("rx") or {}
    if rx.get("rr_connected"):
        return True
    return "ESTABLISHED" in str(rx.get("rr_state") or "").upper()


def neighbor_is_established(vrf: str, neighbor_ip: str) -> bool:
    nip = (neighbor_ip or "").strip()
    if not nip:
        return False
    for row in list_agent_neighbors():
        if str(row.get("address") or "").strip() != nip:
            continue
        rv = str(row.get("vrf") or "default").strip() or "default"
        vrf_n = (vrf or "default").strip() or "default"
        if rv in ("gobgp-rr",) or vrf_n in ("gobgp-rr",):
            return "ESTABLISHED" in str(row.get("state") or "").upper()
        if rv != vrf_n and vrf_n not in ("default", ""):
            continue
        return "ESTABLISHED" in str(row.get("state") or "").upper()
    return False


def list_agent_routes() -> List[Dict[str, Any]]:
    require_agent()
    with _client() as c:
        r = c.get("/api/routes")
        r.raise_for_status()
        return list((r.json() or {}).get("routes") or [])


def list_tx_learned_routes(vrf: str) -> List[Dict[str, Any]]:
    """TX ADJ-IN：从下游运营商学到的路由。"""
    require_agent()
    vrf_n = storage.validate_vrf_name(vrf)
    with _client() as c:
        r = c.get("/api/tx/learned-routes", params={"vrf": vrf_n}, timeout=120.0)
        r.raise_for_status()
        return list((r.json() or {}).get("routes") or [])


def get_peers_freeze_status() -> Dict[str, Any]:
    require_agent()
    with _client() as c:
        r = c.get("/api/peers/freeze-status", timeout=30.0)
        r.raise_for_status()
        return r.json() or {}


def neighbor_session_state(vrf: str, neighbor_ip: str) -> str:
    nip = (neighbor_ip or "").strip()
    vrf_n = storage.validate_vrf_name(vrf)
    if vrf_n == GOBGP_VRF_RR or nip == agent_env_config().get("rr_addr"):
        st = get_status()
        rx = st.get("rx") or {}
        return str(rx.get("rr_state") or "")
    for row in list_agent_neighbors():
        if str(row.get("address") or "").strip() != nip:
            continue
        if storage.validate_vrf_name(str(row.get("vrf") or "default")) != vrf_n:
            continue
        return str(row.get("state") or "")
    return ""


def add_bgp_networks_batch_to_rr(
    prefixes_with_nexthop: list, timeout_s: int = 60
) -> Dict[str, Any]:
    """把路由通告给 RR（RX 方向）。"""
    if not prefixes_with_nexthop:
        return {"added": 0, "failed": 0, "errors": [], "method": "gobgp_rx"}
    nh_default = default_router_id()
    routes = [
        {"prefix": str(p), "nexthop": (str(nh).strip() if nh else nh_default)}
        for p, nh in prefixes_with_nexthop
        if str(p).strip()
    ]
    if not routes:
        return {"added": 0, "failed": 0, "errors": [], "method": "gobgp_rx"}
    require_agent()
    added, failed = 0, 0
    errs: List[str] = []
    chunk = 500
    with _client() as c:
        for i in range(0, len(routes), chunk):
            part = routes[i : i + chunk]
            r = c.post(
                "/api/rr/routes",
                json={"enable": True, "routes": part},
                timeout=float(timeout_s),
            )
            if r.status_code >= 400:
                raise RuntimeError(r.text or f"HTTP {r.status_code}")
            data = r.json() or {}
            added += int(data.get("added") or 0)
            failed += int(data.get("failed") or 0)
            for e in data.get("errors") or []:
                if len(errs) < 20:
                    errs.append(str(e))
    return {"added": added, "failed": failed, "errors": errs, "method": "gobgp_rx"}


def set_bgp_ipv4_network(vrf: str, prefix: str, enable: bool, nexthop: str = "") -> None:
    nh = (nexthop or "").strip() or default_router_id()
    body = {
        "vrf": vrf,
        "enable": bool(enable),
        "routes": [{"prefix": prefix, "nexthop": nh}],
    }
    require_agent()
    with _client() as c:
        r = c.post("/api/tx/routes", json=body, timeout=120.0)
        if r.status_code >= 400:
            raise RuntimeError(r.text or f"HTTP {r.status_code}")
        data = r.json() or {}
        if int(data.get("failed") or 0) > 0:
            raise RuntimeError(str(data.get("errors") or "tx_advertise_failed")[:500])


def add_bgp_networks_batch(
    vrf: str, prefixes_with_nexthop: list, timeout_s: int = 60
) -> Dict[str, Any]:
    if not prefixes_with_nexthop:
        return {"added": 0, "failed": 0, "errors": [], "method": "gobgp_tx"}
    nh_default = default_router_id()
    routes = [
        {"prefix": str(p), "nexthop": (str(nh).strip() if nh else nh_default)}
        for p, nh in prefixes_with_nexthop
        if str(p).strip()
    ]
    if not routes:
        return {"added": 0, "failed": 0, "errors": [], "method": "gobgp_tx"}
    require_agent()
    added, failed = 0, 0
    errs: List[str] = []
    chunk = 500
    with _client() as c:
        for i in range(0, len(routes), chunk):
            part = routes[i : i + chunk]
            r = c.post(
                "/api/tx/routes",
                json={"vrf": vrf, "enable": True, "routes": part},
                timeout=float(timeout_s),
            )
            if r.status_code >= 400:
                raise RuntimeError(r.text or f"HTTP {r.status_code}")
            data = r.json() or {}
            added += int(data.get("added") or 0)
            failed += int(data.get("failed") or 0)
            for e in data.get("errors") or []:
                if len(errs) < 20:
                    errs.append(str(e))
    return {"added": added, "failed": failed, "errors": errs, "method": "gobgp_tx"}
