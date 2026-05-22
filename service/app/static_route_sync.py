"""用户静态路由：``ip route replace`` 下发、对账与探测。"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import storage

logger = logging.getLogger(__name__)

_STATE_NAME = ".static_routes_applied.json"
_IFACE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,14}$")


def dry_run() -> bool:
    raw = (os.environ.get("MTR_STATIC_ROUTE_DRY_RUN") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def auto_apply_on_write() -> bool:
    raw = (os.environ.get("MTR_STATIC_ROUTE_AUTO_APPLY") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def applied_state_path(db_path: Path) -> Path:
    return Path(db_path).expanduser().parent / _STATE_NAME


def _run(argv: List[str], timeout: float = 15.0) -> Tuple[int, str]:
    if dry_run():
        return 0, "dry_run: " + " ".join(argv)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def route_to_dict(route: storage.StaticRoute) -> Dict[str, Any]:
    return {
        "id": route.id,
        "enabled": route.enabled,
        "note": route.note,
        "dst_cidr": route.dst_cidr,
        "gateway_ip": route.gateway_ip,
        "egress_iface": route.egress_iface,
        "pref_src": route.pref_src,
        "install_scope": route.install_scope,
        "routing_mark": route.routing_mark,
        "table_id": route.table_id,
        "metric": route.metric,
        "cross_vrf": route.cross_vrf,
        "nexthop_scope": route.nexthop_scope,
        "nexthop_mark": route.nexthop_mark,
        "created_at": route.created_at,
        "updated_at": route.updated_at,
    }


def _resolve_table_id(route: storage.StaticRoute) -> Optional[int]:
    if route.table_id and route.table_id > 0:
        return int(route.table_id)
    mark = (route.routing_mark or "").strip()
    if mark.isdigit():
        return int(mark)
    return None


def _scope_prefix(route: storage.StaticRoute) -> List[str]:
    scope = (route.install_scope or "main").strip().lower()
    if scope == "vrf":
        vrf = (route.routing_mark or "").strip()
        if not vrf:
            raise ValueError("vrf_scope_requires_routing_mark")
        return ["vrf", vrf]
    if scope == "table":
        tid = _resolve_table_id(route)
        if tid is None:
            raise ValueError("table_scope_requires_table_id")
        return ["table", str(tid)]
    return []


def build_route_argv(route: storage.StaticRoute) -> List[str]:
    """生成 ``ip route replace`` 参数（不含 ``ip route`` 前缀）。"""
    dst = _route_dst_token(route.dst_cidr)
    argv = ["ip", "route", "replace"] + _scope_prefix(route) + [dst]
    gw = (route.gateway_ip or "").strip()
    if gw:
        argv.extend(["via", gw])
    dev = (route.egress_iface or "").strip()
    if dev:
        if not _IFACE_RE.match(dev):
            raise ValueError(f"invalid_iface:{dev}")
        argv.extend(["dev", dev])
        if not gw:
            # 同链路 on-link（回程 2111 的 105.92/30、peer/32 等），与 apply_downstream_transit 一致
            argv.extend(["scope", "link"])
    src = (route.pref_src or "").strip()
    if src:
        argv.extend(["src", src])
    if route.metric and route.metric > 0:
        argv.extend(["metric", str(int(route.metric))])
    return argv


def build_preview_cmds(route: storage.StaticRoute) -> List[str]:
    lines = [" ".join(build_route_argv(route))]
    if route.cross_vrf:
        nh_scope = (route.nexthop_scope or "main").strip().lower()
        nh_mark = (route.nexthop_mark or "").strip()
        hint = f"# cross_vrf: nexthop resolved in {nh_scope}"
        if nh_mark:
            hint += f" ({nh_mark})"
        lines.append(hint)
        if nh_scope == "main" and not (route.gateway_ip or "").strip():
            lines.append(
                "# ensure uplink/default exists in main FIB for via/dev in this route"
            )
    return lines


def withdraw_route(route: storage.StaticRoute, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """停用/删库时从内核撤掉该 FIB 项（OP 独占：停用语义=不再下发且尝试删除）。"""
    return delete_route(route, db_path)


def apply_route(route: storage.StaticRoute, db_path: Optional[Path] = None) -> Dict[str, Any]:
    if not route.enabled:
        return {"ok": True, "skipped": True, "reason": "disabled"}
    argv = build_route_argv(route)
    rc, out = _run(argv)
    result = {"ok": rc == 0, "rc": rc, "argv": argv, "output": out[:800]}
    if rc == 0:
        _save_applied(route.id, argv, db_path)
    return result


def delete_route(route: storage.StaticRoute, db_path: Optional[Path] = None) -> Dict[str, Any]:
    dst = _route_dst_token(route.dst_cidr)
    argv = ["ip", "route", "del"] + _scope_prefix(route) + [dst]
    rc, out = _run(argv)
    if rc != 0 and "No such process" not in out and "not found" not in out.lower():
        # 部分内核返回 "No such process" / 无路由
        pass
    _remove_applied(route.id, db_path)
    return {"ok": rc == 0 or "not found" in out.lower(), "rc": rc, "argv": argv, "output": out[:400]}


def _db_path(db_path: Optional[Path] = None) -> Path:
    if db_path is not None:
        return Path(db_path).expanduser()
    return Path(os.environ.get("MTR_OP_DB", str(Path(__file__).resolve().parent.parent / "data.db"))).expanduser()


def _load_applied_state(path: Path) -> Dict[str, List[str]]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("routes"), dict):
        out: Dict[str, List[str]] = {}
        for k, v in raw["routes"].items():
            if isinstance(v, list):
                out[str(k)] = [str(x) for x in v]
        return out
    return {}


def _save_applied(route_id: int, argv: List[str], db_path: Optional[Path] = None) -> None:
    path = applied_state_path(_db_path(db_path))
    try:
        data = _load_applied_state(path)
        data[str(route_id)] = argv
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"routes": data}, indent=0), encoding="utf-8")
    except OSError as e:
        logger.warning("static_route_sync: save state: %s", e)


def _remove_applied(route_id: int, db_path: Optional[Path] = None) -> None:
    path = applied_state_path(_db_path(db_path))
    try:
        data = _load_applied_state(path)
        data.pop(str(route_id), None)
        path.write_text(json.dumps({"routes": data}, indent=0), encoding="utf-8")
    except OSError:
        pass


def _kernel_show_argv(route: storage.StaticRoute) -> List[str]:
    scope = (route.install_scope or "main").strip().lower()
    if scope == "vrf":
        vrf = (route.routing_mark or "").strip()
        return ["ip", "-j", "route", "show", "vrf", vrf]
    if scope == "table":
        tid = _resolve_table_id(route)
        return ["ip", "-j", "route", "show", "table", str(tid)]
    return ["ip", "-j", "route", "show"]


def _normalize_dst(dst: str) -> str:
    """库内与内核展示统一：default ↔ 0.0.0.0/0。"""
    s = (dst or "").strip()
    if s.lower() == "default":
        return "0.0.0.0/0"
    try:
        net = ipaddress.ip_network(s, strict=False)
        if int(net.prefixlen) == 0:
            return "0.0.0.0/0"
        return str(net)
    except ValueError:
        return s


def _route_dst_token(dst_cidr: str) -> str:
    """``ip route`` CLI 目的：默认路由用 ``default``（与 transit 脚本一致）。"""
    norm = _normalize_dst(dst_cidr)
    return "default" if norm == "0.0.0.0/0" else norm


def _kernel_dst_matches(want_norm: str, kernel_dst: str) -> bool:
    raw = (kernel_dst or "").strip()
    if not raw:
        return False
    if raw.lower() == "default":
        return want_norm == "0.0.0.0/0"
    try:
        return _normalize_dst(raw) == want_norm
    except ValueError:
        return raw == want_norm


def kernel_line_for(route: storage.StaticRoute) -> Optional[str]:
    want = _normalize_dst(route.dst_cidr)
    rc, out = _run(_kernel_show_argv(route), timeout=10.0)
    if rc != 0 or not out.strip():
        return None
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        d = row.get("dst") or ""
        if _kernel_dst_matches(want, d):
            parts = [_route_dst_token(route.dst_cidr)]
            if row.get("gateway"):
                parts.append(f"via {row['gateway']}")
            if row.get("dev"):
                parts.append(f"dev {row['dev']}")
            if row.get("prefsrc"):
                parts.append(f"src {row['prefsrc']}")
            return " ".join(parts)
    return None


def reconcile_one(route: storage.StaticRoute, db_path: Optional[Path] = None) -> str:
    if not route.enabled:
        return "stopped"
    path = applied_state_path(_db_path(db_path))
    applied = _load_applied_state(path).get(str(route.id))
    kernel = kernel_line_for(route)
    if not kernel:
        return "missing"
    if applied:
        expect_argv = build_route_argv(route)
        if applied != expect_argv:
            return "stale"
    return "applied"


def probe_one(route: storage.StaticRoute, probe_dst: Optional[str] = None) -> Dict[str, Any]:
    dst = (probe_dst or route.dst_cidr).strip()
    try:
        net = ipaddress.ip_network(dst, strict=False)
        if net.prefixlen < 32:
            dst = str(net.network_address)
        else:
            dst = str(net.network_address)
    except ValueError:
        dst = dst.split("/")[0]

    scope = (route.install_scope or "main").strip().lower()
    argv: List[str]
    if scope == "vrf":
        vrf = (route.routing_mark or "").strip()
        argv = ["ip", "vrf", "exec", vrf, "ip", "route", "get", dst]
    else:
        argv = ["ip", "route", "get", dst]
        if scope == "table":
            tid = _resolve_table_id(route)
            if tid is not None:
                argv = ["ip", "route", "get", dst, "table", str(tid)]
    src = (route.pref_src or "").strip()
    if src:
        argv.extend(["from", src])
    rc, out = _run(argv, timeout=10.0)
    return {"ok": rc == 0, "rc": rc, "argv": argv, "output": out[:1200]}


def apply_routes(db_path: Path, routes: List[storage.StaticRoute], ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """按库状态同步内核：启用→replace；停用→del（OP 为静态路由唯一控制面）。"""
    id_set = set(ids) if ids else None
    results: List[Dict[str, Any]] = []
    applied_n = 0
    withdrawn_n = 0
    for r in routes:
        if id_set is not None and r.id not in id_set:
            continue
        if r.enabled:
            one = apply_route(r, db_path)
            if one.get("ok") and not one.get("skipped"):
                applied_n += 1
        else:
            one = withdraw_route(r, db_path)
            if one.get("ok"):
                withdrawn_n += 1
        one["id"] = r.id
        results.append(one)
    all_ok = all(x.get("ok") for x in results) if results else True
    return {
        "ok": all_ok,
        "applied": applied_n,
        "withdrawn": withdrawn_n,
        "total": len(results),
        "results": results,
    }


def persist_route_after_db_change(
    row: storage.StaticRoute,
    db_path: Path,
    *,
    previous: Optional[storage.StaticRoute] = None,
) -> Dict[str, Any]:
    """保存/编辑/启停：同步内核。启用→replace；停用→del；改 FIB 键先撤旧项。"""
    if previous is not None and _route_fib_changed(previous, row):
        withdraw_route(previous, db_path)
    if not row.enabled:
        return withdraw_route(row, db_path)
    return apply_route(row, db_path)


# 兼容旧调用名
sync_route_after_db_change = persist_route_after_db_change


def _route_fib_changed(a: storage.StaticRoute, b: storage.StaticRoute) -> bool:
    keys = (
        "dst_cidr",
        "gateway_ip",
        "egress_iface",
        "pref_src",
        "install_scope",
        "routing_mark",
        "table_id",
        "metric",
        "cross_vrf",
        "nexthop_scope",
        "nexthop_mark",
    )
    for k in keys:
        if getattr(a, k) != getattr(b, k):
            return True
    return False


def probe_routes(
    db_path: Path,
    routes: List[storage.StaticRoute],
    ids: Optional[List[int]] = None,
    probe_dst: Optional[str] = None,
) -> Dict[str, Any]:
    id_set = set(ids) if ids else None
    results = []
    for r in routes:
        if id_set is not None and r.id not in id_set:
            continue
        if not r.enabled:
            continue
        one = probe_one(r, probe_dst=probe_dst)
        one["id"] = r.id
        results.append(one)
    return {"results": results}


def delete_routes_kernel(
    routes: List[storage.StaticRoute],
    ids: Optional[List[int]] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    id_set = set(ids) if ids else None
    results = []
    for r in routes:
        if id_set is not None and r.id not in id_set:
            continue
        one = delete_route(r, db_path)
        one["id"] = r.id
        results.append(one)
    return {"results": results}


def list_scopes(db_path: Optional[Path] = None) -> Dict[str, Any]:
    vrfs: List[str] = []
    tables: List[Dict[str, Any]] = []
    ifaces: List[str] = []

    if db_path and db_path.is_file():
        conn = storage.connect(db_path)
        try:
            for v in storage.list_satellite_vrf_names(conn):
                if v and v not in vrfs:
                    vrfs.append(v)
        finally:
            conn.close()

    try:
        from . import kernel_vrf

        for raw in kernel_vrf.list_kernel_vrf_names():
            try:
                vn = storage.validate_vrf_name(raw)
            except ValueError:
                continue
            if vn not in vrfs:
                vrfs.append(vn)
    except Exception:
        pass

    rc, out = _run(["ip", "-4", "rule", "list"], timeout=8.0)
    seen_tables: set = set()
    if rc == 0:
        for line in out.splitlines():
            m = re.search(r"\blookup\s+(\d+)\b", line)
            if m:
                tid = int(m.group(1))
                if tid not in seen_tables:
                    seen_tables.add(tid)
                    tables.append({"id": tid, "name": str(tid)})
    for known in (2110, 2111, 254, 255):
        if known not in seen_tables:
            tables.append({"id": known, "name": str(known)})

    rc2, out2 = _run(["ip", "-j", "link", "show"], timeout=8.0)
    if rc2 == 0 and out2.strip():
        try:
            for row in json.loads(out2):
                name = (row.get("ifname") or "").strip()
                if name and _IFACE_RE.match(name):
                    ifaces.append(name)
        except json.JSONDecodeError:
            pass
    ifaces = sorted(set(ifaces))

    return {"vrfs": sorted(vrfs), "tables": sorted(tables, key=lambda x: x["id"]), "ifaces": ifaces}


def enrich_route(route: storage.StaticRoute, db_path: Path, reconcile: bool = True) -> Dict[str, Any]:
    d = route_to_dict(route)
    d["created_at"] = route.created_at
    d["updated_at"] = route.updated_at
    d["preview_cmds"] = build_preview_cmds(route)
    d["kernel_line"] = kernel_line_for(route) if reconcile else None
    d["sync_state"] = reconcile_one(route, db_path) if reconcile else "unknown"
    return d
