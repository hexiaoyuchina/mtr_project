"""SQLite persistence for global 开关、逐跳规则、ARP 引流配置（旧 gateway_reply_* 表仍保留兼容旧库）。"""
from __future__ import annotations

import ipaddress
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 尝试导入缓存模块（可选，如果不存在则使用纯SQLite）
# 暂时禁用缓存，直接从数据库读取，避免缓存同步问题
HAS_BGP_CACHE = False


def validate_ipv4(s: str) -> str:
    ipaddress.IPv4Address(s.strip())
    return s.strip()


def is_usable_bgp_source_ip(s: str) -> bool:
    """GoBGP 在 admin-down 时可能回报 0.0.0.0；此类值不得覆盖 SQLite 中已配置的 TCP 源。"""
    t = (s or "").strip()
    if not t or t in ("0.0.0.0", "0"):
        return False
    try:
        validate_ipv4(t)
        return True
    except ValueError:
        return False


# OP 中 BGP 邻居「角色」标签（仅存 SQLite，不下发 FRR）
BGP_META_ROLES = frozenset({"upstream", "downstream", "unknown", "rr"})


@dataclass
class GlobalRow:
    hijack_enabled: bool


@dataclass
class ArpSpoofSettings:
    """全局仅总开关；每条冒充网关见 `arp_spoof_targets`。"""
    arp_spoof_enabled: bool


@dataclass
class GatewayReplySettings:
    gateway_reply_enabled: bool
    ingress_ifaces: str
    source_cidrs: str
    reply_icmp_echo: bool
    reply_udp_trace: bool
    miss_action: str


@dataclass
class GatewayReplyPolicy:
    id: int
    name: str
    enabled: bool
    ingress_ifaces: str
    source_cidrs: str
    note: str


@dataclass
class HopReplaceRule:
    id: int
    match_cidr: str
    forged_src: str
    priority: int
    enabled: bool
    note: str
    created_at: str


@dataclass
class ArpSpoofTarget:
    id: int
    spoof_gateway_ip: str
    satellite_vrf: str
    egress_iface: str
    enabled: bool
    policy_mode: str
    policy_cidrs: str
    note: str
    created_at: str = ""


@dataclass
class BgpNeighborMeta:
    vrf: str
    neighbor_ip: str
    role: str
    note: str
    source_ip: str = ""
    advertise_routes: int = 0
    advertise_routes_from: str = ""
    store_received_routes: int = 0


def connect(db_path: Path) -> sqlite3.Connection:
    """连接 SQLite 数据库，启用 WAL 模式提升写入性能。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA cache_size=-100000;")  # 100MB cache (negative value = KB)
    conn.execute("PRAGMA journal_size_limit=524288000;")  # 500MB WAL size limit
    conn.execute("PRAGMA busy_timeout=30000;")  # 30 seconds busy timeout
    conn.execute("PRAGMA foreign_keys=OFF;")  # Disable foreign keys for better performance
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL;")  # Incremental vacuum for better maintenance
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """初始化数据库 schema（幂等）。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS global_config (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          hijack_enabled INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO global_config (id, hijack_enabled) VALUES (1, 0);"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hop_replace_rules (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          match_cidr TEXT NOT NULL,
          forged_src TEXT NOT NULL,
          priority INTEGER NOT NULL DEFAULT 0,
          enabled INTEGER NOT NULL DEFAULT 1,
          note TEXT NOT NULL DEFAULT '',
          delay_first_ms INTEGER NOT NULL DEFAULT 0,
          delay_min_ms INTEGER NOT NULL DEFAULT 0,
          delay_max_ms INTEGER NOT NULL DEFAULT 64,
          icmp_ip_ttl INTEGER NOT NULL DEFAULT 64,
          loss_percent INTEGER NOT NULL DEFAULT 0,
          jitter_ms INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arp_spoof_settings (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          arp_spoof_enabled INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO arp_spoof_settings (id, arp_spoof_enabled) VALUES (1, 0);"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arp_spoof_targets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          spoof_gateway_ip TEXT NOT NULL UNIQUE,
          satellite_vrf TEXT NOT NULL DEFAULT '',
          egress_iface TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 1,
          policy_mode TEXT NOT NULL DEFAULT 'gateway_only',
          policy_cidrs TEXT NOT NULL DEFAULT '',
          note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        """
    )

    try:
        conn.execute("ALTER TABLE arp_spoof_targets ADD COLUMN satellite_vrf TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "ALTER TABLE arp_spoof_targets ADD COLUMN created_at TEXT NOT NULL "
            "DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "UPDATE arp_spoof_targets SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE created_at IS NULL OR trim(created_at) = ''"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_reply_settings (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          gateway_reply_enabled INTEGER NOT NULL DEFAULT 0,
          ingress_ifaces TEXT NOT NULL DEFAULT '',
          source_cidrs TEXT NOT NULL DEFAULT '',
          reply_icmp_echo INTEGER NOT NULL DEFAULT 1,
          reply_udp_trace INTEGER NOT NULL DEFAULT 0,
          miss_action TEXT NOT NULL DEFAULT 'accept'
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO gateway_reply_settings (id, gateway_reply_enabled, ingress_ifaces, source_cidrs, reply_icmp_echo, reply_udp_trace, miss_action) VALUES (1, 0, '', '', 1, 0, 'accept');"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_reply_policies (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          enabled INTEGER NOT NULL DEFAULT 1,
          ingress_ifaces TEXT NOT NULL DEFAULT '',
          source_cidrs TEXT NOT NULL DEFAULT '',
          note TEXT NOT NULL DEFAULT ''
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bgp_neighbor_meta (
          vrf TEXT NOT NULL,
          neighbor_ip TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'unknown',
          note TEXT NOT NULL DEFAULT '',
          source_ip TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (vrf, neighbor_ip)
        );
        """
    )

    try:
        conn.execute("ALTER TABLE bgp_neighbor_meta ADD COLUMN source_ip TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("ALTER TABLE bgp_neighbor_meta ADD COLUMN advertise_routes INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("ALTER TABLE bgp_neighbor_meta ADD COLUMN advertise_routes_from TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute(
            "ALTER TABLE bgp_neighbor_meta ADD COLUMN created_at TEXT NOT NULL "
            "DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        )
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute(
            "ALTER TABLE bgp_neighbor_meta ADD COLUMN store_received_routes INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bgp_learned_routes (
          vrf TEXT NOT NULL,
          prefix TEXT NOT NULL,
          nexthop TEXT NOT NULL DEFAULT '',
          neighbor_ip TEXT NOT NULL DEFAULT '',
          remote_as INTEGER NOT NULL DEFAULT 0,
          role TEXT NOT NULL DEFAULT 'unknown',
          as_path TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL,
          PRIMARY KEY (vrf, prefix, nexthop)
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bgp_upstream_route_cache (
          learn_vrf TEXT NOT NULL,
          prefix TEXT NOT NULL,
          nexthop TEXT NOT NULL DEFAULT '',
          neighbor_ip TEXT NOT NULL DEFAULT '',
          remote_as INTEGER NOT NULL DEFAULT 0,
          as_path TEXT NOT NULL DEFAULT '',
          last_live_at TEXT NOT NULL,
          PRIMARY KEY (learn_vrf, prefix)
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bgp_sticky_frr (
          advert_vrf TEXT NOT NULL,
          prefix TEXT NOT NULL,
          installed_at TEXT NOT NULL,
          PRIMARY KEY (advert_vrf, prefix)
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bgp_rib_sync_state (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          last_sync_at TEXT,
          last_ok INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO bgp_rib_sync_state (id, last_sync_at, last_ok, last_error) VALUES (1, NULL, 0, '');"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bgp_peer_snapshot (
          vrf TEXT NOT NULL,
          neighbor_ip TEXT NOT NULL,
          window_type TEXT NOT NULL DEFAULT 'upstream',
          frozen INTEGER NOT NULL DEFAULT 0,
          session_established INTEGER NOT NULL DEFAULT 0,
          route_count INTEGER NOT NULL DEFAULT 0,
          last_sync_at TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (vrf, neighbor_ip)
        );
        """
    )
    try:
        conn.execute(
            "ALTER TABLE bgp_learned_routes ADD COLUMN route_window TEXT NOT NULL DEFAULT 'upstream'"
        )
    except sqlite3.OperationalError:
        pass

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vpn_links (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          link_type TEXT NOT NULL,
          vrf TEXT NOT NULL DEFAULT 'vrf2103',
          endpoint TEXT NOT NULL DEFAULT '',
          iface_name TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 1,
          desired_up INTEGER NOT NULL DEFAULT 1,
          priority INTEGER NOT NULL DEFAULT 100,
          config_json TEXT NOT NULL DEFAULT '{}',
          last_error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        """
    )

    # 创建索引提升查询性能
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bgp_learned_routes_vrf ON bgp_learned_routes(vrf);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bgp_learned_routes_prefix ON bgp_learned_routes(prefix);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bgp_upstream_cache_vrf ON bgp_upstream_route_cache(learn_vrf);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bgp_neighbor_meta_vrf ON bgp_neighbor_meta(vrf);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bgp_learned_routes_neighbor_ip ON bgp_learned_routes(neighbor_ip);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bgp_learned_routes_vrf_neighbor ON bgp_learned_routes(vrf, neighbor_ip);
        """
    )

    conn.commit()


def seed_defaults(conn: sqlite3.Connection) -> None:
    pass


def validate_vrf_name(vrf: str) -> str:
    v = (vrf or "default").strip()
    if not v:
        return "default"
    return v


# === Global Config ===

def get_global(conn: sqlite3.Connection) -> GlobalRow:
    return get_global_config(conn)

def get_global_config(conn: sqlite3.Connection) -> GlobalRow:
    row = conn.execute("SELECT hijack_enabled FROM global_config WHERE id = 1").fetchone()
    if row:
        return GlobalRow(hijack_enabled=int(row["hijack_enabled"]) != 0)
    return GlobalRow(hijack_enabled=False)


def set_global_hijack_enabled(conn: sqlite3.Connection, enabled: bool) -> None:
    conn.execute("UPDATE global_config SET hijack_enabled = ? WHERE id = 1", (1 if enabled else 0,))
    conn.commit()


def set_global(conn: sqlite3.Connection, enabled: bool) -> None:
    """写入 MTR/ICMP 逐跳替换总开关（``api_global_put`` 使用）。"""
    set_global_hijack_enabled(conn, enabled)


def _hop_rule_row_dict(conn: sqlite3.Connection, rule_id: int) -> Optional[Dict[str, Any]]:
    row = get_hop_replace_rule(conn, rule_id)
    if not row:
        return None
    return {
        "id": row.id,
        "match_cidr": row.match_cidr,
        "forged_src": row.forged_src,
        "priority": row.priority,
        "enabled": row.enabled,
        "note": row.note,
        "created_at": row.created_at,
    }


def add_hop_rule(
    conn: sqlite3.Connection,
    match_cidr: str,
    forged_src: str,
    priority: int = 0,
    enabled: bool = True,
    note: str = "",
) -> Dict[str, Any]:
    rid = insert_hop_replace_rule(conn, match_cidr, forged_src, priority, enabled, note)
    out = _hop_rule_row_dict(conn, int(rid))
    if not out:
        raise ValueError("hop_rule_insert_failed")
    return out


def update_hop_rule(
    conn: sqlite3.Connection,
    rule_id: int,
    match_cidr: Optional[str] = None,
    forged_src: Optional[str] = None,
    priority: Optional[int] = None,
    enabled: Optional[bool] = None,
    note: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    ok = update_hop_replace_rule(conn, rule_id, match_cidr, forged_src, priority, enabled, note)
    if not ok:
        return None
    return _hop_rule_row_dict(conn, rule_id)


def delete_hop_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    return delete_hop_replace_rule(conn, rule_id)


def set_arp_spoof_settings(conn: sqlite3.Connection, arp_spoof_enabled: bool) -> ArpSpoofSettings:
    set_arp_spoof_enabled(conn, arp_spoof_enabled)
    return get_arp_spoof_settings(conn)


def list_bgp_distinct_learned_vrfs(conn: sqlite3.Connection) -> List[str]:
    return list_bgp_learned_routes_vrfs(conn)


# === Hop Replace Rules ===

def list_hop_replace_rules(conn: sqlite3.Connection) -> List[HopReplaceRule]:
    out = []
    for row in conn.execute("SELECT * FROM hop_replace_rules ORDER BY priority DESC, id"):
        out.append(
            HopReplaceRule(
                id=int(row["id"]),
                match_cidr=str(row["match_cidr"]),
                forged_src=str(row["forged_src"]),
                priority=int(row["priority"]),
                enabled=bool(row["enabled"]),
                note=str(row["note"]),
                created_at=str(row["created_at"]),
            )
        )
    return out


def list_hop_rules_enabled(conn: sqlite3.Connection) -> List[HopReplaceRule]:
    return [r for r in list_hop_replace_rules(conn) if r.enabled]


def get_hop_replace_rule(conn: sqlite3.Connection, rule_id: int) -> Optional[HopReplaceRule]:
    row = conn.execute("SELECT * FROM hop_replace_rules WHERE id = ?", (rule_id,)).fetchone()
    if not row:
        return None
    return HopReplaceRule(
        id=int(row["id"]),
        match_cidr=str(row["match_cidr"]),
        forged_src=str(row["forged_src"]),
        priority=int(row["priority"]),
        enabled=bool(row["enabled"]),
        note=str(row["note"]),
        created_at=str(row["created_at"]),
    )


def insert_hop_replace_rule(
    conn: sqlite3.Connection,
    match_cidr: str,
    forged_src: str,
    priority: int = 0,
    enabled: bool = True,
    note: str = "",
) -> int:
    ipaddress.IPv4Address(forged_src.strip())
    cidr = match_cidr.strip()
    if "/" not in cidr:
        cidr += "/32"
    ipaddress.ip_network(cidr, strict=False)
    now = datetime.utcnow().isoformat() + "Z"
    cur = conn.execute(
        """
        INSERT INTO hop_replace_rules (match_cidr, forged_src, priority, enabled, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (cidr, forged_src.strip(), priority, 1 if enabled else 0, note, now),
    )
    conn.commit()
    return cur.lastrowid


def update_hop_replace_rule(
    conn: sqlite3.Connection,
    rule_id: int,
    match_cidr: Optional[str] = None,
    forged_src: Optional[str] = None,
    priority: Optional[int] = None,
    enabled: Optional[bool] = None,
    note: Optional[str] = None,
) -> bool:
    updates = []
    params = []
    if match_cidr is not None:
        cidr = match_cidr.strip()
        if "/" not in cidr:
            cidr += "/32"
        ipaddress.ip_network(cidr, strict=False)
        updates.append("match_cidr = ?")
        params.append(cidr)
    if forged_src is not None:
        ipaddress.IPv4Address(forged_src.strip())
        updates.append("forged_src = ?")
        params.append(forged_src.strip())
    if priority is not None:
        updates.append("priority = ?")
        params.append(priority)
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if enabled else 0)
    if note is not None:
        updates.append("note = ?")
        params.append(note)
    if not updates:
        return False
    params.append(rule_id)
    conn.execute(f"UPDATE hop_replace_rules SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    return conn.execute("SELECT 1 FROM hop_replace_rules WHERE id = ?", (rule_id,)).fetchone() is not None


def delete_hop_replace_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    conn.execute("DELETE FROM hop_replace_rules WHERE id = ?", (rule_id,))
    conn.commit()
    return conn.execute("SELECT 1 FROM hop_replace_rules WHERE id = ?", (rule_id,)).fetchone() is None


# === ARP Spoof Settings ===

def get_arp_spoof_settings(conn: sqlite3.Connection) -> ArpSpoofSettings:
    row = conn.execute("SELECT arp_spoof_enabled FROM arp_spoof_settings WHERE id = 1").fetchone()
    if row:
        return ArpSpoofSettings(arp_spoof_enabled=bool(row["arp_spoof_enabled"]))
    return ArpSpoofSettings(arp_spoof_enabled=False)


def set_arp_spoof_enabled(conn: sqlite3.Connection, enabled: bool) -> None:
    conn.execute("UPDATE arp_spoof_settings SET arp_spoof_enabled = ? WHERE id = 1", (1 if enabled else 0,))
    conn.commit()


# === ARP Spoof Targets ===

def _arp_target_from_row(row: sqlite3.Row) -> ArpSpoofTarget:
    return ArpSpoofTarget(
        id=int(row["id"]),
        spoof_gateway_ip=str(row["spoof_gateway_ip"]),
        satellite_vrf=str(row["satellite_vrf"]) if "satellite_vrf" in row.keys() else "",
        egress_iface=str(row["egress_iface"]),
        enabled=bool(row["enabled"]),
        policy_mode=str(row["policy_mode"]),
        policy_cidrs=str(row["policy_cidrs"]),
        note=str(row["note"]),
        created_at=str(row["created_at"]) if "created_at" in row.keys() else "",
    )


def list_arp_spoof_targets(conn: sqlite3.Connection) -> List[ArpSpoofTarget]:
    out = []
    for row in conn.execute("SELECT * FROM arp_spoof_targets ORDER BY id"):
        out.append(_arp_target_from_row(row))
    return out


def get_arp_spoof_target(conn: sqlite3.Connection, target_id: int) -> Optional[ArpSpoofTarget]:
    row = conn.execute("SELECT * FROM arp_spoof_targets WHERE id = ?", (target_id,)).fetchone()
    if not row:
        return None
    return _arp_target_from_row(row)


def list_arp_spoof_targets_enabled(conn: sqlite3.Connection) -> List[ArpSpoofTarget]:
    return [t for t in list_arp_spoof_targets(conn) if t.enabled]


def insert_arp_spoof_target(
    conn: sqlite3.Connection,
    spoof_gateway_ip: str,
    satellite_vrf: Optional[str] = None,
    egress_iface: str = "",
    enabled: bool = True,
    policy_mode: str = "gateway_only",
    policy_cidrs: str = "",
    note: str = "",
) -> int:
    validate_ipv4(spoof_gateway_ip)
    cur = conn.execute(
        """
        INSERT INTO arp_spoof_targets (
          spoof_gateway_ip, satellite_vrf, egress_iface, enabled, policy_mode, policy_cidrs, note, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        """,
        (spoof_gateway_ip.strip(), (satellite_vrf or "").strip(), egress_iface.strip(), 1 if enabled else 0, policy_mode.strip(), policy_cidrs.strip(), note),
    )
    conn.commit()
    return cur.lastrowid


def update_arp_spoof_target(
    conn: sqlite3.Connection,
    target_id: int,
    spoof_gateway_ip: Optional[str] = None,
    satellite_vrf: Optional[str] = None,
    egress_iface: Optional[str] = None,
    enabled: Optional[bool] = None,
    policy_mode: Optional[str] = None,
    policy_cidrs: Optional[str] = None,
    note: Optional[str] = None,
) -> bool:
    updates = []
    params = []
    if spoof_gateway_ip is not None:
        validate_ipv4(spoof_gateway_ip)
        updates.append("spoof_gateway_ip = ?")
        params.append(spoof_gateway_ip.strip())
    if satellite_vrf is not None:
        updates.append("satellite_vrf = ?")
        params.append(satellite_vrf.strip())
    if egress_iface is not None:
        updates.append("egress_iface = ?")
        params.append(egress_iface.strip())
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if enabled else 0)
    if policy_mode is not None:
        updates.append("policy_mode = ?")
        params.append(policy_mode.strip())
    if policy_cidrs is not None:
        updates.append("policy_cidrs = ?")
        params.append(policy_cidrs.strip())
    if note is not None:
        updates.append("note = ?")
        params.append(note)
    if not updates:
        return False
    params.append(target_id)
    conn.execute(f"UPDATE arp_spoof_targets SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    return conn.execute("SELECT 1 FROM arp_spoof_targets WHERE id = ?", (target_id,)).fetchone() is not None


def delete_arp_spoof_target(conn: sqlite3.Connection, target_id: int) -> bool:
    conn.execute("DELETE FROM arp_spoof_targets WHERE id = ?", (target_id,))
    conn.commit()
    return conn.execute("SELECT 1 FROM arp_spoof_targets WHERE id = ?", (target_id,)).fetchone() is None


# === Gateway Reply Settings ===

def get_gateway_reply_settings(conn: sqlite3.Connection) -> GatewayReplySettings:
    row = conn.execute(
        "SELECT gateway_reply_enabled, ingress_ifaces, source_cidrs, reply_icmp_echo, reply_udp_trace, miss_action FROM gateway_reply_settings WHERE id = 1"
    ).fetchone()
    if row:
        return GatewayReplySettings(
            gateway_reply_enabled=bool(row["gateway_reply_enabled"]),
            ingress_ifaces=str(row["ingress_ifaces"]),
            source_cidrs=str(row["source_cidrs"]),
            reply_icmp_echo=bool(row["reply_icmp_echo"]),
            reply_udp_trace=bool(row["reply_udp_trace"]),
            miss_action=str(row["miss_action"]),
        )
    return GatewayReplySettings(
        gateway_reply_enabled=False,
        ingress_ifaces="",
        source_cidrs="",
        reply_icmp_echo=True,
        reply_udp_trace=False,
        miss_action="accept",
    )


def update_gateway_reply_settings(
    conn: sqlite3.Connection,
    gateway_reply_enabled: Optional[bool] = None,
    ingress_ifaces: Optional[str] = None,
    source_cidrs: Optional[str] = None,
    reply_icmp_echo: Optional[bool] = None,
    reply_udp_trace: Optional[bool] = None,
    miss_action: Optional[str] = None,
) -> None:
    updates = []
    params = []
    if gateway_reply_enabled is not None:
        updates.append("gateway_reply_enabled = ?")
        params.append(1 if gateway_reply_enabled else 0)
    if ingress_ifaces is not None:
        updates.append("ingress_ifaces = ?")
        params.append(ingress_ifaces)
    if source_cidrs is not None:
        updates.append("source_cidrs = ?")
        params.append(source_cidrs)
    if reply_icmp_echo is not None:
        updates.append("reply_icmp_echo = ?")
        params.append(1 if reply_icmp_echo else 0)
    if reply_udp_trace is not None:
        updates.append("reply_udp_trace = ?")
        params.append(1 if reply_udp_trace else 0)
    if miss_action is not None:
        updates.append("miss_action = ?")
        params.append(miss_action)
    if updates:
        conn.execute(f"UPDATE gateway_reply_settings SET {', '.join(updates)} WHERE id = 1", params)
        conn.commit()


# === Gateway Reply Policies ===

def list_gateway_reply_policies(conn: sqlite3.Connection) -> List[GatewayReplyPolicy]:
    out = []
    for row in conn.execute("SELECT * FROM gateway_reply_policies ORDER BY id"):
        out.append(
            GatewayReplyPolicy(
                id=int(row["id"]),
                name=str(row["name"]),
                enabled=bool(row["enabled"]),
                ingress_ifaces=str(row["ingress_ifaces"]),
                source_cidrs=str(row["source_cidrs"]),
                note=str(row["note"]),
            )
        )
    return out


def get_gateway_reply_policy(conn: sqlite3.Connection, policy_id: int) -> Optional[GatewayReplyPolicy]:
    row = conn.execute("SELECT * FROM gateway_reply_policies WHERE id = ?", (policy_id,)).fetchone()
    if not row:
        return None
    return GatewayReplyPolicy(
        id=int(row["id"]),
        name=str(row["name"]),
        enabled=bool(row["enabled"]),
        ingress_ifaces=str(row["ingress_ifaces"]),
        source_cidrs=str(row["source_cidrs"]),
        note=str(row["note"]),
    )


def insert_gateway_reply_policy(
    conn: sqlite3.Connection,
    name: str,
    enabled: bool = True,
    ingress_ifaces: str = "",
    source_cidrs: str = "",
    note: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO gateway_reply_policies (name, enabled, ingress_ifaces, source_cidrs, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (name.strip(), 1 if enabled else 0, ingress_ifaces, source_cidrs, note),
    )
    conn.commit()
    return cur.lastrowid


def update_gateway_reply_policy(
    conn: sqlite3.Connection,
    policy_id: int,
    name: Optional[str] = None,
    enabled: Optional[bool] = None,
    ingress_ifaces: Optional[str] = None,
    source_cidrs: Optional[str] = None,
    note: Optional[str] = None,
) -> bool:
    updates = []
    params = []
    if name is not None:
        updates.append("name = ?")
        params.append(name.strip())
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if enabled else 0)
    if ingress_ifaces is not None:
        updates.append("ingress_ifaces = ?")
        params.append(ingress_ifaces)
    if source_cidrs is not None:
        updates.append("source_cidrs = ?")
        params.append(source_cidrs)
    if note is not None:
        updates.append("note = ?")
        params.append(note)
    if not updates:
        return False
    params.append(policy_id)
    conn.execute(f"UPDATE gateway_reply_policies SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    return conn.execute("SELECT 1 FROM gateway_reply_policies WHERE id = ?", (policy_id,)).fetchone() is not None


def delete_gateway_reply_policy(conn: sqlite3.Connection, policy_id: int) -> bool:
    conn.execute("DELETE FROM gateway_reply_policies WHERE id = ?", (policy_id,))
    conn.commit()
    return conn.execute("SELECT 1 FROM gateway_reply_policies WHERE id = ?", (policy_id,)).fetchone() is None


# === BGP Neighbor Meta ===

def downstream_neighbor_ip_for_vrf(conn: sqlite3.Connection, vrf: str) -> Optional[str]:
    """卫星 VRF 内在 BGP 管理配置的下游邻居 IP（用于 ipvlan 收敛写 VRF 路由）。"""
    v = validate_vrf_name(vrf)
    meta = get_bgp_neighbor_meta_map(conn, v)
    if not meta:
        return None
    downstream: List[str] = []
    other: List[str] = []
    for nip, tup in meta.items():
        role = (tup[0] or "").strip().lower() if tup else ""
        if role in {"downstream", "upstream"}:
            if role == "downstream":
                downstream.append(nip)
            else:
                other.append(nip)
        else:
            other.append(nip)
    pick = downstream[0] if downstream else (other[0] if len(other) == 1 else None)
    if pick:
        return validate_ipv4(pick)
    return None


def get_bgp_neighbor_meta_map(conn: sqlite3.Connection, vrf: str) -> Dict[str, tuple]:
    """返回 {neighbor_ip: (role, note, source_ip, advertise_routes, advertise_routes_from)}。"""
    v = validate_vrf_name(vrf)
    out = {}
    for row in conn.execute("SELECT neighbor_ip, role, note, source_ip, advertise_routes, advertise_routes_from FROM bgp_neighbor_meta WHERE vrf = ?", (v,)):
        out[str(row["neighbor_ip"])] = (
            str(row["role"]),
            str(row["note"]),
            str(row["source_ip"]),
            int(row["advertise_routes"]) if "advertise_routes" in row.keys() else 0,
            str(row["advertise_routes_from"]) if "advertise_routes_from" in row.keys() else ""
        )
    return out


def lookup_bgp_neighbor_meta_for_nexthop(conn: sqlite3.Connection, vrf: str, nexthop: str) -> tuple:
    """查找下一跳对应的邻居元数据，返回 (neighbor_ip, role)。"""
    if not nexthop:
        return ("", "unknown")
    v = validate_vrf_name(vrf)
    nh = nexthop.strip()
    row = conn.execute(
        "SELECT neighbor_ip, role FROM bgp_neighbor_meta WHERE vrf = ? AND neighbor_ip = ?",
        (v, nh),
    ).fetchone()
    if row:
        return (str(row["neighbor_ip"]), str(row["role"]))
    return ("", "unknown")


def upsert_bgp_neighbor_meta(conn: sqlite3.Connection, vrf: str, neighbor_ip: str, role: str, note: str = "", source_ip: str = "", advertise_routes: int = 0, advertise_routes_from: str = "") -> None:
    v = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    r = str(role).strip().lower() if role else "unknown"
    if r not in BGP_META_ROLES:
        r = "unknown"
    sip = str(source_ip or "").strip()
    ar = int(advertise_routes) if advertise_routes else 0
    arf = str(advertise_routes_from or "").strip()
    conn.execute(
        """
        INSERT OR REPLACE INTO bgp_neighbor_meta (vrf, neighbor_ip, role, note, source_ip, advertise_routes, advertise_routes_from, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (v, nip, r, note, sip, ar, arf),
    )
    conn.commit()


def set_bgp_neighbor_meta(conn: sqlite3.Connection, vrf: str, neighbor_ip: str, role: str, note: str = "", update_source: Optional[str] = None) -> None:
    """设置 BGP 邻居元数据；``update_source`` 无效时保留库内已有 TCP 源。"""
    v = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    existing = get_bgp_neighbor_meta_map(conn, v).get(nip)
    sip = str(existing[2] or "").strip() if existing and len(existing) > 2 else ""
    if update_source is not None and is_usable_bgp_source_ip(update_source):
        sip = validate_ipv4(update_source)
    ar = int(existing[3]) if existing and len(existing) > 3 else 0
    arf = str(existing[4] or "").strip() if existing and len(existing) > 4 else ""
    upsert_bgp_neighbor_meta(conn, v, nip, role, note, source_ip=sip, advertise_routes=ar, advertise_routes_from=arf)


def delete_bgp_neighbor_meta(conn: sqlite3.Connection, vrf: str, neighbor_ip: str) -> None:
    v = validate_vrf_name(vrf)
    nip = (neighbor_ip or "").strip()
    if nip:
        conn.execute("DELETE FROM bgp_neighbor_meta WHERE vrf = ? AND neighbor_ip = ?", (v, nip))
        conn.commit()


def update_bgp_neighbor_advertise_routes(conn: sqlite3.Connection, vrf: str, neighbor_ip: str, advertise_routes: int, advertise_routes_from: str) -> None:
    """更新邻居的路由通告设置。"""
    v = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    ar = int(advertise_routes) if advertise_routes else 0
    arf = str(advertise_routes_from or "").strip()
    conn.execute(
        "UPDATE bgp_neighbor_meta SET advertise_routes = ?, advertise_routes_from = ? WHERE vrf = ? AND neighbor_ip = ?",
        (ar, arf, v, nip)
    )
    conn.commit()


def get_bgp_neighbor_store_received_routes(conn: sqlite3.Connection, vrf: str, neighbor_ip: str) -> int:
    v = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    row = conn.execute(
        "SELECT store_received_routes FROM bgp_neighbor_meta WHERE vrf = ? AND neighbor_ip = ?",
        (v, nip),
    ).fetchone()
    if not row:
        return 0
    try:
        return int(row["store_received_routes"] or 0)
    except (KeyError, TypeError):
        return 0


def update_bgp_neighbor_store_received_routes(conn: sqlite3.Connection, vrf: str, neighbor_ip: str, store_received_routes: int) -> None:
    v = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    sr = 1 if store_received_routes else 0
    conn.execute(
        "UPDATE bgp_neighbor_meta SET store_received_routes = ? WHERE vrf = ? AND neighbor_ip = ?",
        (sr, v, nip),
    )
    conn.commit()


def get_route_advertise_sources(conn: sqlite3.Connection, vrf: str, target_neighbor: str) -> List[str]:
    """
    动态获取需要通告路由的来源邻居列表。
    
    逻辑：
    1. 查找所有其他邻居（排除目标邻居自己）
    2. 返回这些邻居的neighbor_ip列表
    
    注意：在所有VRF中查找
    
    例如：
    - target_neighbor = '139.159.43.249'
    - 返回除了'139.159.43.249'之外的所有邻居IP
    """
    nip = validate_ipv4(target_neighbor)
    
    # 在所有VRF中查找所有其他邻居
    sources = []
    for row in conn.execute(
        "SELECT DISTINCT neighbor_ip FROM bgp_neighbor_meta WHERE neighbor_ip != ?",
        (nip,)
    ):
        sources.append(str(row[0]))
    
    return sources


def get_all_other_neighbors(conn: sqlite3.Connection, target_neighbor: str) -> List[str]:
    """
    获取除了目标邻居之外的所有其他邻居。
    
    返回：所有其他邻居的IP列表
    """
    nip = validate_ipv4(target_neighbor)
    
    sources = []
    for row in conn.execute(
        "SELECT DISTINCT neighbor_ip FROM bgp_neighbor_meta WHERE neighbor_ip != ?",
        (nip,)
    ):
        sources.append(str(row[0]))
    
    return sources


def get_downstream_neighbors(conn: sqlite3.Connection, rr_ip: str) -> List[tuple]:
    """
    获取RR的所有下游邻居。
    
    逻辑：
    - 查找所有source_ip匹配rr_ip的邻居
    - 排除RR自身
    
    返回：[(vrf, neighbor_ip), ...]
    """
    # 查找所有VRF中source_ip匹配的下游邻居
    downstream = []
    for row in conn.execute(
        "SELECT vrf, neighbor_ip FROM bgp_neighbor_meta WHERE source_ip = ? AND neighbor_ip != ?",
        (rr_ip, rr_ip)
    ):
        downstream.append((str(row[0]), str(row[1])))
    
    return downstream


def get_all_rr_neighbors(conn: sqlite3.Connection) -> List[tuple]:
    """
    获取所有RR角色的邻居。
    
    返回：[(vrf, neighbor_ip, source_ip), ...]
    """
    rr_list = []
    for row in conn.execute(
        "SELECT vrf, neighbor_ip, source_ip FROM bgp_neighbor_meta WHERE role IN ('rr', 'RR')"
    ):
        rr_list.append((str(row[0]), str(row[1]), str(row[2]) if row[2] else ""))
    
    return rr_list


def list_bgp_neighbor_meta(conn: sqlite3.Connection, vrf: str) -> List[BgpNeighborMeta]:
    v = validate_vrf_name(vrf)
    out = []
    for row in conn.execute("SELECT * FROM bgp_neighbor_meta WHERE vrf = ? ORDER BY neighbor_ip", (v,)):
        out.append(
            BgpNeighborMeta(
                vrf=str(row["vrf"]),
                neighbor_ip=str(row["neighbor_ip"]),
                role=str(row["role"]),
                note=str(row["note"]),
                source_ip=str(row["source_ip"]) if "source_ip" in row.keys() else "",
                advertise_routes=int(row["advertise_routes"]) if "advertise_routes" in row.keys() else 0,
                advertise_routes_from=str(row["advertise_routes_from"]) if "advertise_routes_from" in row.keys() else "",
            )
        )
    return out


def bgp_neighbor_meta_role_map_from_env() -> Dict[str, str]:
    """
    从环境变量 ``MTR_BGP_ROLE_MAP`` 读取邻居角色映射，格式：
    ``10.133.153.204:upstream,10.133.151.204:downstream``

    未设置时使用实验室默认：ROS(153.204) 上游；Linux 下游 151.204 / 152.204 / 152.205。
    """
    raw = (os.environ.get("MTR_BGP_ROLE_MAP") or "").strip()
    if not raw:
        raw = (
            "139.159.43.249:rr,"
            "139.159.43.208:downstream,"
            "10.133.153.204:upstream,"
            "10.133.151.204:downstream,"
            "10.133.152.204:downstream,"
            "10.133.152.205:downstream"
        )
    out: Dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            continue
        ip, role = part.rsplit(":", 1)
        ip = ip.strip()
        role = role.strip().lower()
        if role in BGP_META_ROLES:
            out[ip] = role
    return out


def apply_bgp_role_map_from_env(conn: sqlite3.Connection, vrf: str) -> None:
    """将环境变量中的角色映射写入数据库。"""
    role_map = bgp_neighbor_meta_role_map_from_env()
    v = validate_vrf_name(vrf)
    for ip, role in role_map.items():
        upsert_bgp_neighbor_meta(conn, v, ip, role)


def ensure_bgp_neighbor_meta_row(conn: sqlite3.Connection, vrf: str, neighbor_ip: str) -> None:
    """FRR 已有邻居但库中无行时插入 unknown（不覆盖已有角色）。"""
    v = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    conn.execute(
        """
        INSERT OR IGNORE INTO bgp_neighbor_meta (vrf, neighbor_ip, role, note)
        VALUES (?, ?, 'unknown', '')
        """,
        (v, nip),
    )
    conn.commit()


def parse_bgp_db_presets_from_env() -> List[tuple[str, str, str]]:
    """
    ``MTR_BGP_DB_PRESETS``：``vrf:neighbor_ip:role`` 逗号分隔。
    未设置时使用实验室默认（vrf2103 上游 ROS；default/vrf2102 下游 Linux 201）。
    """
    raw = (os.environ.get("MTR_BGP_DB_PRESETS") or "").strip()
    if not raw:
        # RR 不在此预设：须由 OP「BGP 管理」手工创建（角色 RR）
        raw = (
            "gobgp-tx:139.159.43.208:downstream,"
            "vrf2103:10.133.153.204:upstream,"
            "default:10.133.152.204:downstream,"
            "vrf2102:10.133.152.204:downstream"
        )
    out: List[tuple[str, str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part or part.count(":") < 2:
            continue
        vrf, ip, role = part.split(":", 2)
        vrf = vrf.strip()
        ip = ip.strip()
        role = role.strip().lower()
        if role not in BGP_META_ROLES:
            continue
        try:
            validate_ipv4(ip)
        except ValueError:
            continue
        out.append((validate_vrf_name(vrf), ip, role))
    return out


def apply_bgp_db_presets(conn: sqlite3.Connection) -> List[str]:
    """将预设角色写入 SQLite（覆盖为手动录入，供学习/粘性通告识别上下游）。"""
    applied: List[str] = []
    for vrf, ip, role in parse_bgp_db_presets_from_env():
        upsert_bgp_neighbor_meta(conn, vrf, ip, role, note="preset")
        applied.append(f"{vrf}:{ip}:{role}")
    return applied


def default_bgp_role_hints() -> Dict[str, str]:
    """按邻居 IP 的默认角色提示（未写入库时 UI 展示 hint）。"""
    return bgp_neighbor_meta_role_map_from_env()


def cache_all_routes_in_learn_vrf() -> bool:
    """为 True 时，学习 VRF（默认 vrf2103）内 RIB 快照中的**全部**前缀写入上游持久缓存。"""
    raw = (os.environ.get("MTR_BGP_CACHE_ALL_LEARN_VRF") or "1").strip().lower()
    return raw not in {"0", "false", "no"}


def build_upstream_cache_rows(
    learn_vrf: str,
    vrf: str,
    rows: List[tuple],
    upstream_ips: set[str],
    ts: str,
) -> List[tuple]:
    """
    从一次 RIB 快照行生成 ``bgp_upstream_route_cache`` 批量写入元组。
    默认缓存学习 VRF 内全部前缀；``MTR_BGP_CACHE_ALL_LEARN_VRF=0`` 时仅缓存 role=upstream 或上游邻居 IP 匹配项。
    """
    if vrf != learn_vrf or not rows:
        return []
    cache_all = cache_all_routes_in_learn_vrf()
    out: List[tuple] = []
    seen: set[str] = set()
    for tup in rows:
        if len(tup) < 7:
            continue
        prefix = str(tup[0])
        nh = str(tup[1] or "").strip()
        peer = str(tup[2] or "").strip()
        role_t = str(tup[4] or "").strip().lower()
        if not cache_all and role_t != "upstream" and nh not in upstream_ips and peer not in upstream_ips:
            continue
        if prefix in seen:
            continue
        seen.add(prefix)
        out.append((vrf, prefix, nh, peer, int(tup[3] or 0), str(tup[5] or "")[:512], ts))
    return out


# === BGP Peer snapshot / bidirectional ===

def set_bgp_peer_frozen(
    conn: sqlite3.Connection, vrf: str, neighbor_ip: str, window_type: str, frozen: bool
) -> None:
    v = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    wt = (window_type or "upstream").strip().lower()
    conn.execute(
        """
        INSERT INTO bgp_peer_snapshot (vrf, neighbor_ip, window_type, frozen, last_sync_at)
        VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ON CONFLICT(vrf, neighbor_ip) DO UPDATE SET
          window_type=excluded.window_type,
          frozen=excluded.frozen,
          last_sync_at=excluded.last_sync_at
        """,
        (v, nip, wt, 1 if frozen else 0),
    )
    conn.commit()


def is_bgp_peer_frozen(conn: sqlite3.Connection, vrf: str, neighbor_ip: str) -> bool:
    v = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    row = conn.execute(
        "SELECT frozen FROM bgp_peer_snapshot WHERE vrf = ? AND neighbor_ip = ?",
        (v, nip),
    ).fetchone()
    if not row:
        return False
    return int(row["frozen"]) != 0


def touch_bgp_peer_snapshot(
    conn: sqlite3.Connection,
    vrf: str,
    neighbor_ip: str,
    window_type: str,
    *,
    route_count: int = 0,
    session_established: Optional[bool] = None,
) -> None:
    v = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    wt = (window_type or "upstream").strip().lower()
    se = None if session_established is None else (1 if session_established else 0)
    if se is None:
        conn.execute(
            """
            INSERT INTO bgp_peer_snapshot (vrf, neighbor_ip, window_type, route_count, last_sync_at)
            VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(vrf, neighbor_ip) DO UPDATE SET
              route_count=excluded.route_count,
              last_sync_at=excluded.last_sync_at
            """,
            (v, nip, wt, int(route_count)),
        )
    else:
        conn.execute(
            """
            INSERT INTO bgp_peer_snapshot (vrf, neighbor_ip, window_type, route_count, session_established, last_sync_at)
            VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(vrf, neighbor_ip) DO UPDATE SET
              route_count=excluded.route_count,
              session_established=excluded.session_established,
              last_sync_at=excluded.last_sync_at
            """,
            (v, nip, wt, int(route_count), se),
        )
    conn.commit()


def get_bgp_peer_frozen_map(conn: sqlite3.Connection) -> Dict[tuple, bool]:
    out: Dict[tuple, bool] = {}
    for row in conn.execute("SELECT vrf, neighbor_ip, frozen FROM bgp_peer_snapshot"):
        out[(str(row["vrf"]), str(row["neighbor_ip"]))] = int(row["frozen"]) != 0
    return out


def count_routes_for_peer(conn: sqlite3.Connection, vrf: str, neighbor_ip: str) -> int:
    v = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM bgp_learned_routes WHERE vrf = ? AND neighbor_ip = ?",
        (v, nip),
    ).fetchone()
    return int(row["c"]) if row else 0


def replace_bgp_learned_routes_for_peer(
    conn: sqlite3.Connection,
    vrf: str,
    neighbor_ip: str,
    rows: List[tuple],
) -> None:
    """按 peer 覆盖快照（定时同步）；``rows`` 每项 7 元组或 8 元组（末位 route_window）。"""
    v = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "DELETE FROM bgp_learned_routes WHERE vrf = ? AND neighbor_ip = ?",
            (v, nip),
        )
        if rows:
            norm = []
            for r in rows:
                if len(r) >= 8:
                    rw = str(r[7] or "upstream")
                    core = r[:7]
                else:
                    rw = "upstream"
                    core = r
                norm.append((v,) + tuple(core) + (rw,))
            conn.executemany(
                "INSERT INTO bgp_learned_routes "
                "(vrf, prefix, nexthop, neighbor_ip, remote_as, role, as_path, updated_at, route_window) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                norm,
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def route_window_for_bgp_role(role: str) -> str:
    """本端邻居角色对应的 SQLite 学习窗（与 ``bgp_learned_routes.route_window`` 一致）。"""
    r = (role or "").strip().lower()
    if r == "downstream":
        return "downstream"
    return "upstream"


def count_bgp_routes_for_peer_window(
    conn: sqlite3.Connection,
    vrf: str,
    neighbor_ip: str,
    role: str,
) -> int:
    vrf_n = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    rw = route_window_for_bgp_role(role)
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM bgp_learned_routes WHERE vrf = ? AND neighbor_ip = ? AND route_window = ?",
        (vrf_n, nip, rw),
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def iter_bgp_routes_for_peer_window(
    conn: sqlite3.Connection,
    vrf: str,
    neighbor_ip: str,
    role: str,
    batch_size: int = 10000,
):
    """本窗缓存：该 VRF + 对端邻居 + 窗类型在库中学到的路由（定时同步快照）。"""
    vrf_n = validate_vrf_name(vrf)
    nip = validate_ipv4(neighbor_ip)
    rw = route_window_for_bgp_role(role)
    sql = (
        "SELECT prefix, nexthop FROM bgp_learned_routes "
        "WHERE vrf = ? AND neighbor_ip = ? AND route_window = ? ORDER BY prefix"
    )
    params = (vrf_n, nip, rw)
    offset = 0
    while True:
        chunk = conn.execute(sql + " LIMIT ? OFFSET ?", params + (batch_size, offset)).fetchall()
        if not chunk:
            break
        for row in chunk:
            yield (str(row["prefix"]), str(row["nexthop"] or ""))
        if len(chunk) < batch_size:
            break
        offset += batch_size


def iter_bgp_routes_for_advertise_source(
    conn: sqlite3.Connection,
    source_spec: str,
    batch_size: int = 10000,
):
    """解析通告来源：neighbor IP、``@upstream``、``@downstream``（遗留 API，新 UI 用 ``iter_bgp_routes_for_peer_window``）。"""
    spec = (source_spec or "").strip()
    if not spec:
        return
    if spec.lower() in {"@upstream", "upstream", "rr"}:
        sql = (
            "SELECT prefix, nexthop FROM bgp_learned_routes "
            "WHERE route_window = 'upstream' OR role IN ('rr','upstream') ORDER BY prefix"
        )
        params: tuple = ()
    elif spec.lower() in {"@downstream", "downstream"}:
        sql = (
            "SELECT prefix, nexthop FROM bgp_learned_routes "
            "WHERE route_window = 'downstream' OR role = 'downstream' ORDER BY prefix"
        )
        params = ()
    else:
        nip = validate_ipv4(spec)
        sql = (
            "SELECT prefix, nexthop FROM bgp_learned_routes WHERE neighbor_ip = ? ORDER BY prefix"
        )
        params = (nip,)
    offset = 0
    while True:
        chunk = conn.execute(sql + " LIMIT ? OFFSET ?", params + (batch_size, offset)).fetchall()
        if not chunk:
            break
        for row in chunk:
            yield (str(row["prefix"]), str(row["nexthop"] or ""))
        if len(chunk) < batch_size:
            break
        offset += batch_size


# === BGP Learned Routes ===

def replace_bgp_learned_routes_for_vrf(
    conn: sqlite3.Connection,
    vrf: str,
    rows: List[tuple],
) -> None:
    """
    用一次 FRR 快照覆盖该 VRF 下本地缓存（先删后插）。``rows`` 每项：
    ``(prefix, nexthop, neighbor_ip, remote_as, role, as_path, updated_at)``。
    """
    vrf_n = validate_vrf_name(vrf)
    # 关闭自动提交，手动控制事务
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("DELETE FROM bgp_learned_routes WHERE vrf = ?", (vrf_n,))
        if rows:
            norm = []
            for r in rows:
                core = r[:7] if len(r) >= 7 else r
                rw = str(r[7]) if len(r) >= 8 else "upstream"
                norm.append((vrf_n,) + tuple(core) + (rw,))
            conn.executemany(
                "INSERT INTO bgp_learned_routes "
                "(vrf, prefix, nexthop, neighbor_ip, remote_as, role, as_path, updated_at, route_window) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                norm,
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def delete_bgp_learned_routes_not_in_vrfs(conn: sqlite3.Connection, vrfs: set[str]) -> None:
    """删除已不在 FRR BGP 实例列表中的 VRF 的缓存行（防止残留）。"""
    if not vrfs:
        conn.execute("DELETE FROM bgp_learned_routes")
        conn.commit()
        return
    placeholders = ",".join("?" * len(vrfs))
    conn.execute(f"DELETE FROM bgp_learned_routes WHERE vrf NOT IN ({placeholders})", tuple(sorted(vrfs)))
    conn.commit()


def delete_bgp_learned_routes_by_neighbor_ip(conn: sqlite3.Connection, neighbor_ip: str) -> int:
    """删除来源邻居IP为neighbor_ip的所有路由数据。返回删除的行数。"""
    validate_ipv4(neighbor_ip)
    batch_size = 1000
    total_deleted = 0
    while True:
        cursor = conn.execute(
            "DELETE FROM bgp_learned_routes WHERE neighbor_ip = ? LIMIT ?",
            (neighbor_ip, batch_size)
        )
        deleted = cursor.rowcount
        if deleted == 0:
            break
        total_deleted += deleted
        conn.commit()
    return total_deleted


def _learned_routes_where_sql(
    vrf: Optional[str] = None,
    neighbor_ip: Optional[str] = None,
    route_window: Optional[str] = None,
) -> tuple[str, list]:
    """构建 bgp_learned_routes 查询条件（仅 SQLite；route_window=upstream|downstream）。"""
    clauses: list[str] = []
    params: list = []
    if vrf:
        clauses.append("vrf = ?")
        params.append(validate_vrf_name(vrf))
    if neighbor_ip:
        clauses.append("neighbor_ip = ?")
        params.append(neighbor_ip.strip())
    rw = (route_window or "").strip().lower()
    if rw in {"upstream", "downstream"}:
        clauses.append(
            "(route_window = ? OR (COALESCE(route_window,'') = '' AND role IN ('rr','upstream') AND ? = 'upstream') "
            "OR (COALESCE(route_window,'') = '' AND role = 'downstream' AND ? = 'downstream'))"
        )
        params.extend([rw, rw, rw])
    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def summarize_learned_routes_by_window(conn: sqlite3.Connection) -> Dict[str, int]:
    out = {"upstream": 0, "downstream": 0, "total": 0}
    for row in conn.execute(
        """
        SELECT
          CASE
            WHEN route_window = 'downstream'
              OR (COALESCE(route_window, '') = '' AND role = 'downstream') THEN 'downstream'
            ELSE 'upstream'
          END AS win,
          COUNT(*) AS c
        FROM bgp_learned_routes
        GROUP BY win
        """
    ):
        win = str(row[0] or "upstream")
        c = int(row[1] or 0)
        if win == "downstream":
            out["downstream"] = c
        else:
            out["upstream"] = c
        out["total"] += c
    return out


def list_bgp_peer_snapshots_brief(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = []
    for row in conn.execute(
        """
        SELECT vrf, neighbor_ip, window_type, frozen, session_established, route_count, last_sync_at
        FROM bgp_peer_snapshot ORDER BY window_type, vrf, neighbor_ip
        """
    ):
        rows.append(
            {
                "vrf": str(row["vrf"]),
                "neighbor_ip": str(row["neighbor_ip"]),
                "window_type": str(row["window_type"] or ""),
                "frozen": int(row["frozen"]) != 0,
                "session_established": int(row["session_established"]) != 0,
                "route_count": int(row["route_count"] or 0),
                "last_sync_at": str(row["last_sync_at"] or ""),
            }
        )
    return rows


def list_bgp_learned_routes(
    conn: sqlite3.Connection,
    vrf: Optional[str] = None,
    neighbor_ip: Optional[str] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    route_window: Optional[str] = None,
) -> List[sqlite3.Row]:
    rw_filter = (route_window or "").strip().lower() or None
    # 优先从缓存读取
    if HAS_BGP_CACHE and not rw_filter:
        try:
            cache = bgp_route_cache.get_global_cache()
            vrf_n = validate_vrf_name(vrf) if vrf else None
            offset = (page - 1) * page_size if page and page_size else 0
            routes = cache.get_routes(
                vrf=vrf_n,
                neighbor_ip=neighbor_ip.strip() if neighbor_ip else None,
                limit=page_size,
                offset=offset
            )
            # 转换为 Row 格式
            return [create_route_row(r) for r in routes]
        except Exception:
            pass

    # 回退到数据库
    where, params = _learned_routes_where_sql(vrf, neighbor_ip, rw_filter)
    sql = (
        "SELECT vrf, prefix, nexthop, neighbor_ip, remote_as, role, as_path, updated_at, "
        "COALESCE(NULLIF(route_window,''), CASE WHEN role = 'downstream' THEN 'downstream' ELSE 'upstream' END) AS route_window "
        "FROM bgp_learned_routes " + where + " ORDER BY route_window, vrf, prefix, nexthop"
    )
    if page is not None and page_size is not None:
        sql += " LIMIT ? OFFSET ?"
        params.append(page_size)
        params.append((page - 1) * page_size)
    return list(conn.execute(sql, params))


def create_route_row(route) -> sqlite3.Row:
    """将缓存中的路由对象转换为 sqlite3.Row"""
    class FakeRow:
        def __init__(self, data):
            self._data = data
        def __getitem__(self, key):
            return self._data[key]
        def keys(self):
            return [
                'vrf', 'prefix', 'nexthop', 'neighbor_ip', 'remote_as', 'role', 'as_path',
                'updated_at', 'route_window',
            ]

    rw = 'downstream' if (route.role or '').lower() == 'downstream' else 'upstream'
    return FakeRow({
        'vrf': '',
        'prefix': route.prefix,
        'nexthop': route.nexthop,
        'neighbor_ip': route.neighbor_ip,
        'remote_as': route.remote_as,
        'role': route.role,
        'as_path': route.as_path,
        'updated_at': route.updated_at,
        'route_window': rw,
    })


def count_bgp_learned_routes(
    conn: sqlite3.Connection,
    vrf: Optional[str] = None,
    neighbor_ip: Optional[str] = None,
    route_window: Optional[str] = None,
) -> int:
    rw_filter = (route_window or "").strip().lower() or None
    # 优先从缓存读取
    if HAS_BGP_CACHE and not rw_filter:
        try:
            cache = bgp_route_cache.get_global_cache()
            vrf_n = validate_vrf_name(vrf) if vrf else None
            return cache.count_routes(
                vrf=vrf_n,
                neighbor_ip=neighbor_ip.strip() if neighbor_ip else None
            )
        except Exception:
            pass

    # 回退到数据库
    where, params = _learned_routes_where_sql(vrf, neighbor_ip, rw_filter)
    row = conn.execute("SELECT COUNT(*) FROM bgp_learned_routes " + where, params).fetchone()
    return int(row[0]) if row else 0


def iter_bgp_learned_routes_by_neighbor_ip(
    conn: sqlite3.Connection,
    neighbor_ip: str,
    batch_size: int = 10000,
):
    """
    高效批量查询来源邻居IP对应的所有路由。
    使用生成器模式避免一次性加载百万条数据到内存。
    返回: (prefix, nexthop) 元组的迭代器
    """
    # 优先从缓存读取
    if HAS_BGP_CACHE:
        try:
            cache = bgp_route_cache.get_global_cache()
            routes = cache.get_routes(neighbor_ip=neighbor_ip.strip())
            for route in routes:
                yield (route.prefix, route.nexthop)
            return
        except Exception:
            pass

    # 回退到数据库
    validate_ipv4(neighbor_ip)
    offset = 0
    while True:
        rows = list(conn.execute(
            "SELECT prefix, nexthop FROM bgp_learned_routes WHERE neighbor_ip = ? ORDER BY prefix LIMIT ? OFFSET ?",
            (neighbor_ip.strip(), batch_size, offset)
        ))
        if not rows:
            break
        for row in rows:
            yield (str(row[0]), str(row[1]) if row[1] else "")
        offset += batch_size


def count_bgp_learned_routes_by_neighbor_ip(conn: sqlite3.Connection, neighbor_ip: str) -> int:
    """统计来源邻居IP对应的路由数量。"""
    # 优先从缓存读取
    if HAS_BGP_CACHE:
        try:
            cache = bgp_route_cache.get_global_cache()
            return cache.count_routes(neighbor_ip=neighbor_ip.strip())
        except Exception:
            pass

    # 回退到数据库
    validate_ipv4(neighbor_ip)
    row = conn.execute(
        "SELECT COUNT(*) FROM bgp_learned_routes WHERE neighbor_ip = ?",
        (neighbor_ip.strip(),)
    ).fetchone()
    return int(row[0]) if row else 0


def list_bgp_learned_routes_vrfs(conn: sqlite3.Connection) -> List[str]:
    return [str(r[0]) for r in conn.execute("SELECT DISTINCT vrf FROM bgp_learned_routes ORDER BY vrf").fetchall()]


def list_satellite_vrf_names(conn: sqlite3.Connection) -> List[str]:
    """获取所有已配置的satellite_vrf名称列表，用于下拉选择。"""
    return sorted({
        str(r[0]) for r in conn.execute(
            "SELECT DISTINCT satellite_vrf FROM arp_spoof_targets WHERE satellite_vrf != '' ORDER BY satellite_vrf"
        ).fetchall()
    })


def list_bgp_distinct_learned_neighbor_ips(conn: sqlite3.Connection) -> List[str]:
    return [
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT neighbor_ip FROM (
              SELECT neighbor_ip FROM bgp_learned_routes WHERE TRIM(COALESCE(neighbor_ip, '')) != ''
              UNION
              SELECT neighbor_ip FROM bgp_upstream_route_cache WHERE TRIM(COALESCE(neighbor_ip, '')) != ''
            ) ORDER BY neighbor_ip
            """
        ).fetchall()
    ]


# === BGP Upstream Route Cache ===

def upsert_bgp_upstream_route_cache(
    conn: sqlite3.Connection,
    learn_vrf: str,
    prefix: str,
    nexthop: str,
    neighbor_ip: str,
    remote_as: int,
    as_path: str,
    last_live_at: str,
) -> None:
    """ROS/上游邻居当前在 RIB 中出现的前缀：写入或刷新 last_live_at（不因上游断连而删除）。"""
    v = validate_vrf_name(learn_vrf)
    pfx = str(ipaddress.ip_network(prefix.strip(), strict=False))
    conn.execute(
        "INSERT INTO bgp_upstream_route_cache (learn_vrf, prefix, nexthop, neighbor_ip, remote_as, as_path, last_live_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(learn_vrf, prefix) DO UPDATE SET "
        "nexthop=excluded.nexthop, neighbor_ip=excluded.neighbor_ip, remote_as=excluded.remote_as, "
        "as_path=excluded.as_path, last_live_at=excluded.last_live_at",
        (v, pfx, (nexthop or "").strip(), (neighbor_ip or "").strip(), int(remote_as), (as_path or "")[:512], last_live_at),
    )


def bulk_upsert_bgp_upstream_route_cache(
    conn: sqlite3.Connection,
    rows: List[tuple],
) -> None:
    """批量 upsert 上游路由缓存，提升性能。"""
    if not rows:
        return
    # 预处理数据：验证 VRF、规范化前缀
    prepared_rows = []
    for row in rows:
        vrf, prefix, nexthop, neighbor_ip, remote_as, as_path, last_live_at = row
        v = validate_vrf_name(vrf)
        pfx = str(ipaddress.ip_network(prefix.strip(), strict=False))
        prepared_rows.append(
            (v, pfx, (nexthop or "").strip(), (neighbor_ip or "").strip(), int(remote_as), (as_path or "")[:512], last_live_at)
        )
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.executemany(
            "INSERT INTO bgp_upstream_route_cache (learn_vrf, prefix, nexthop, neighbor_ip, remote_as, as_path, last_live_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(learn_vrf, prefix) DO UPDATE SET "
            "nexthop=excluded.nexthop, neighbor_ip=excluded.neighbor_ip, remote_as=excluded.remote_as, "
            "as_path=excluded.as_path, last_live_at=excluded.last_live_at",
            prepared_rows,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def list_bgp_upstream_cache_rows(conn: sqlite3.Connection, learn_vrf: str) -> List[sqlite3.Row]:
    v = validate_vrf_name(learn_vrf)
    return list(
        conn.execute(
            "SELECT learn_vrf, prefix, nexthop, neighbor_ip, remote_as, as_path, last_live_at "
            "FROM bgp_upstream_route_cache WHERE learn_vrf = ? ORDER BY prefix",
            (v,),
        )
    )


def prune_bgp_upstream_route_cache_before(conn: sqlite3.Connection, learn_vrf: str, cutoff: str) -> int:
    v = validate_vrf_name(learn_vrf)
    cur = conn.execute("DELETE FROM bgp_upstream_route_cache WHERE learn_vrf = ? AND last_live_at < ?", (v, cutoff))
    conn.commit()
    return cur.rowcount


# === BGP Sticky FRR ===

def list_bgp_sticky_frr_prefixes(conn: sqlite3.Connection, advert_vrf: str) -> List[str]:
    v = validate_vrf_name(advert_vrf)
    rows = conn.execute("SELECT prefix FROM bgp_sticky_frr WHERE advert_vrf = ?", (v,)).fetchall()
    return [str(r["prefix"]) for r in rows]


def add_bgp_sticky_frr(conn: sqlite3.Connection, advert_vrf: str, prefix: str, installed_at: str) -> None:
    v = validate_vrf_name(advert_vrf)
    pfx = str(ipaddress.ip_network(prefix.strip(), strict=False))
    conn.execute(
        "INSERT OR REPLACE INTO bgp_sticky_frr (advert_vrf, prefix, installed_at) VALUES (?, ?, ?)",
        (v, pfx, installed_at),
    )
    conn.commit()


def remove_bgp_sticky_frr(conn: sqlite3.Connection, advert_vrf: str, prefix: str) -> None:
    v = validate_vrf_name(advert_vrf)
    pfx = str(ipaddress.ip_network(prefix.strip(), strict=False))
    conn.execute("DELETE FROM bgp_sticky_frr WHERE advert_vrf = ? AND prefix = ?", (v, pfx))
    conn.commit()


# === BGP RIB Sync State ===

def get_bgp_rib_sync_state(conn: sqlite3.Connection) -> tuple:
    row = conn.execute("SELECT last_sync_at, last_ok, last_error FROM bgp_rib_sync_state WHERE id = 1").fetchone()
    if row:
        return (str(row["last_sync_at"] or ""), bool(row["last_ok"]), str(row["last_error"] or ""))
    return ("", False, "")


def set_bgp_rib_sync_state(conn: sqlite3.Connection, last_sync_at: str, last_ok: bool, last_error: str) -> None:
    conn.execute(
        "UPDATE bgp_rib_sync_state SET last_sync_at = ?, last_ok = ?, last_error = ? WHERE id = 1",
        (last_sync_at, 1 if last_ok else 0, last_error),
    )
    conn.commit()


# === VPN Links ===

def list_vpn_links(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    out = []
    for row in conn.execute("SELECT * FROM vpn_links ORDER BY name"):
        out.append({
            "id": int(row["id"]),
            "name": str(row["name"]),
            "link_type": str(row["link_type"]),
            "vrf": str(row["vrf"]),
            "endpoint": str(row["endpoint"]),
            "iface_name": str(row["iface_name"]),
            "enabled": bool(row["enabled"]),
            "desired_up": bool(row["desired_up"]),
            "priority": int(row["priority"]),
            "config_json": json.loads(row["config_json"] or "{}"),
            "last_error": str(row["last_error"]),
            "created_at": str(row["created_at"]),
        })
    return out


def get_vpn_link(conn: sqlite3.Connection, link_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM vpn_links WHERE id = ?", (link_id,)).fetchone()
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "link_type": str(row["link_type"]),
        "vrf": str(row["vrf"]),
        "endpoint": str(row["endpoint"]),
        "iface_name": str(row["iface_name"]),
        "enabled": bool(row["enabled"]),
        "desired_up": bool(row["desired_up"]),
        "priority": int(row["priority"]),
        "config_json": json.loads(row["config_json"] or "{}"),
        "last_error": str(row["last_error"]),
        "created_at": str(row["created_at"]),
    }


def insert_vpn_link(
    conn: sqlite3.Connection,
    name: str,
    link_type: str,
    vrf: str = "vrf2103",
    endpoint: str = "",
    iface_name: str = "",
    enabled: bool = True,
    desired_up: bool = True,
    priority: int = 100,
    config_json: Dict[str, Any] = None,
    last_error: str = "",
) -> int:
    now = datetime.utcnow().isoformat() + "Z"
    cur = conn.execute(
        """
        INSERT INTO vpn_links (name, link_type, vrf, endpoint, iface_name, enabled, desired_up, priority, config_json, last_error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name.strip(),
            link_type.strip(),
            validate_vrf_name(vrf),
            endpoint.strip(),
            iface_name.strip(),
            1 if enabled else 0,
            1 if desired_up else 0,
            priority,
            json.dumps(config_json or {}),
            last_error,
            now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_vpn_link(
    conn: sqlite3.Connection,
    link_id: int,
    name: Optional[str] = None,
    link_type: Optional[str] = None,
    vrf: Optional[str] = None,
    endpoint: Optional[str] = None,
    iface_name: Optional[str] = None,
    enabled: Optional[bool] = None,
    desired_up: Optional[bool] = None,
    priority: Optional[int] = None,
    config_json: Optional[Dict[str, Any]] = None,
    last_error: Optional[str] = None,
) -> bool:
    updates = []
    params = []
    if name is not None:
        updates.append("name = ?")
        params.append(name.strip())
    if link_type is not None:
        updates.append("link_type = ?")
        params.append(link_type.strip())
    if vrf is not None:
        updates.append("vrf = ?")
        params.append(validate_vrf_name(vrf))
    if endpoint is not None:
        updates.append("endpoint = ?")
        params.append(endpoint.strip())
    if iface_name is not None:
        updates.append("iface_name = ?")
        params.append(iface_name.strip())
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if enabled else 0)
    if desired_up is not None:
        updates.append("desired_up = ?")
        params.append(1 if desired_up else 0)
    if priority is not None:
        updates.append("priority = ?")
        params.append(priority)
    if config_json is not None:
        updates.append("config_json = ?")
        params.append(json.dumps(config_json))
    if last_error is not None:
        updates.append("last_error = ?")
        params.append(last_error)
    if not updates:
        return False
    params.append(link_id)
    conn.execute(f"UPDATE vpn_links SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    return conn.execute("SELECT 1 FROM vpn_links WHERE id = ?", (link_id,)).fetchone() is not None


def delete_vpn_link(conn: sqlite3.Connection, link_id: int) -> bool:
    conn.execute("DELETE FROM vpn_links WHERE id = ?", (link_id,))
    conn.commit()
    return conn.execute("SELECT 1 FROM vpn_links WHERE id = ?", (link_id,)).fetchone() is None


# === Migration Helpers ===

def _migrate_arp_spoof_targets(conn: sqlite3.Connection) -> None:
    """迁移旧版单行配置到多行 arp_spoof_targets 表。"""
    row = conn.execute("SELECT spoof_gateway_ips FROM arp_spoof_settings WHERE id = 1").fetchone()
    if not row:
        return
    ips = str(row["spoof_gateway_ips"] or "").strip()
    if not ips:
        return
    for ip_str in ips.split(","):
        ip = ip_str.strip()
        if ip:
            try:
                validate_ipv4(ip)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO arp_spoof_targets (
                      spoof_gateway_ip, egress_iface, enabled, policy_mode, created_at
                    ) VALUES (?, '', 1, 'gateway_only', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                    """,
                    (ip,),
                )
            except ValueError:
                pass
    conn.execute("ALTER TABLE arp_spoof_settings DROP COLUMN spoof_gateway_ips")
    conn.commit()
