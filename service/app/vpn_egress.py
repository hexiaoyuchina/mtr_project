"""VPN 出口下发：GRE / OpenVPN / L2TP（vrf2103）与策略路由（ip rule + table）。"""
from __future__ import annotations

import ipaddress
import logging
import os
import platform
import shutil
import stat
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import storage

logger = logging.getLogger(__name__)

POLICY_RT_TABLE_BASE = int(os.environ.get("MTR_VPN_POLICY_TABLE_BASE", "33700"))
POLICY_RULE_PREF_BASE = int(os.environ.get("MTR_VPN_POLICY_RULE_PREF_BASE", "28000"))


def vpn_apply_enabled() -> bool:
    if os.environ.get("MTR_OP_VPN_APPLY", "1").strip().lower() in {"0", "false", "no"}:
        return False
    return platform.system() == "Linux"


def _run(args: List[str], timeout: int = 90) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def _sudo_prefix() -> List[str]:
    try:
        euid = os.geteuid()
    except AttributeError:
        return []
    if euid == 0:
        return []
    if shutil.which("sudo"):
        return ["sudo", "-n"]
    return []


def _ip(args: List[str], timeout: int = 60) -> Tuple[int, str, str]:
    return _run(_sudo_prefix() + ["ip"] + args, timeout=timeout)


def policy_table_id(policy_id: int) -> int:
    return POLICY_RT_TABLE_BASE + int(policy_id)


def policy_rule_pref(policy_id: int) -> int:
    return POLICY_RULE_PREF_BASE + int(policy_id)


def _iface_exists(name: str) -> bool:
    return (Path("/sys/class/net") / name).is_dir()


def _read_iface_bytes(iface: str) -> Tuple[int, int]:
    base = Path("/sys/class/net") / iface / "statistics"
    try:
        rx = int((base / "rx_bytes").read_text().strip())
        tx = int((base / "tx_bytes").read_text().strip())
        return rx, tx
    except OSError:
        return 0, 0


def refresh_link_stats(conn: sqlite3.Connection, link: Dict[str, Any]) -> None:
    ifn = (link.get("iface_name") or "").strip()
    if not ifn or not _iface_exists(ifn):
        return
    rx, tx = _read_iface_bytes(ifn)
    storage.set_vpn_link_status(conn, int(link["id"]), rx_bytes=rx, tx_bytes=tx)


def ping_in_vrf(vrf: str, target: str, count: int = 3) -> Dict[str, Any]:
    """在指定 VRF 内 ping（用于连通性测试）。"""
    vrf_n = storage.validate_vrf_name(vrf)
    c = max(1, min(int(count), 20))
    args = _sudo_prefix() + ["ip", "vrf", "exec", vrf_n, "ping", "-c", str(c), "-W", "2", target]
    code, out, err = _run(args, timeout=10 + c * 2)
    return {"ok": code == 0, "returncode": code, "stdout": out, "stderr": err, "vrf": vrf_n, "target": target}


def _teardown_policy_kernel(pol: Dict[str, Any]) -> None:
    pid = int(pol["id"])
    tid = policy_table_id(pid)
    pref = policy_rule_pref(pid)
    _ip(["rule", "del", "pref", str(pref)])
    _ip(["route", "flush", "table", str(tid)])


def _pick_primary_iface_for_link(conn: sqlite3.Connection, link: Dict[str, Any]) -> Optional[str]:
    ifn = (link.get("iface_name") or "").strip()
    if ifn and _iface_exists(ifn):
        return ifn
    return None


def _tunnel_up(conn: sqlite3.Connection, link: Dict[str, Any]) -> bool:
    return _pick_primary_iface_for_link(conn, link) is not None


def apply_policy(conn: sqlite3.Connection, pol: Dict[str, Any], links_by_id: Dict[int, Dict[str, Any]]) -> None:
    lid = int(pol["vpn_link_id"])
    link = links_by_id.get(lid)
    if not link:
        return
    tid = policy_table_id(int(pol["id"]))
    pref = policy_rule_pref(int(pol["id"]))
    dst = pol["dst_cidr"]
    src_raw = (pol.get("src_cidr") or "").strip()
    src_first = src_raw.split(",")[0].strip() if src_raw else ""
    fail = (pol.get("fail_action") or "fallback").lower()

    primary = _pick_primary_iface_for_link(conn, link)
    backup_if: Optional[str] = None
    bid = pol.get("backup_link_id")
    if bid is not None and fail == "switch_backup":
        bl = links_by_id.get(int(bid))
        if bl:
            backup_if = _pick_primary_iface_for_link(conn, bl)

    use_iface: Optional[str] = None
    if primary:
        use_iface = primary
    elif backup_if:
        use_iface = backup_if
    elif fail == "deny":
        _ip(["route", "replace", dst, "prohibit", "table", str(tid)])
    else:
        # fallback: 不装策略路由
        return

    if use_iface:
        _ip(["route", "replace", dst, "dev", use_iface, "table", str(tid)])

    if src_first:
        try:
            net = str(ipaddress.ip_network(src_first, strict=False))
        except ValueError:
            try:
                net = str(ipaddress.ip_network(f"{src_first}/32", strict=False))
            except ValueError:
                net = ""
        if net:
            _ip(["rule", "add", "from", net, "to", dst, "lookup", str(tid), "pref", str(pref)])
        else:
            _ip(["rule", "add", "to", dst, "lookup", str(tid), "pref", str(pref)])
    else:
        _ip(["rule", "add", "to", dst, "lookup", str(tid), "pref", str(pref)])


def teardown_gre(link: Dict[str, Any]) -> None:
    ifn = (link.get("iface_name") or "").strip()
    if not ifn:
        return
    _ip(["tunnel", "del", ifn])


def apply_gre(conn: sqlite3.Connection, link: Dict[str, Any]) -> None:
    cfg = (link.get("config") or {}).get("gre") or {}
    remote = (cfg.get("remote") or "").strip() or (link.get("endpoint") or "").split(":")[0].strip()
    if not remote:
        storage.set_vpn_link_status(
            conn,
            int(link["id"]),
            actual_status="down",
            last_error="gre_missing_remote",
        )
        storage.append_vpn_event_log(conn, "vpn", int(link["id"]), "GRE: missing remote in config.gre.remote or endpoint")
        return
    local = (cfg.get("local") or "0.0.0.0").strip()
    ttl = int(cfg.get("ttl", 64))
    mtu = int(cfg.get("mtu", 1476))
    ifn = (link.get("iface_name") or "").strip()
    vrf = storage.validate_vrf_name(link.get("vrf") or "vrf2103")

    teardown_gre(link)
    args_t = ["tunnel", "add", ifn, "mode", "gre", "remote", remote, "ttl", str(ttl)]
    if local and local not in ("0.0.0.0", "0", ""):
        args_t.extend(["local", local])
    code, out, err = _ip(args_t)
    if code != 0:
        storage.set_vpn_link_status(conn, int(link["id"]), actual_status="down", last_error=f"gre_tunnel_add: {err or out}")
        storage.append_vpn_event_log(conn, "vpn", int(link["id"]), f"GRE add failed: {err or out}")
        return
    _ip(["link", "set", ifn, "master", vrf])
    _ip(["link", "set", ifn, "mtu", str(mtu), "up"])
    storage.set_vpn_link_status(conn, int(link["id"]), actual_status="up", last_error="")
    storage.append_vpn_event_log(conn, "vpn", int(link["id"]), f"GRE up {ifn} remote={remote} vrf={vrf}")


def _vpn_data_dir() -> Path:
    return Path(os.environ.get("MTR_OP_DATA", str(Path(__file__).resolve().parent.parent / "data"))) / "vpn"


def teardown_openvpn(link: Dict[str, Any]) -> None:
    d = _vpn_data_dir()
    pidf = d / f"openvpn-{link['id']}.pid"
    if pidf.is_file():
        try:
            pid = int(pidf.read_text().strip().split()[0])
            _run(_sudo_prefix() + ["kill", str(pid)], timeout=10)
        except (ValueError, OSError):
            pass
        try:
            pidf.unlink()
        except OSError:
            pass
    ifn = (link.get("iface_name") or "").strip()
    if ifn and _iface_exists(ifn):
        _ip(["link", "delete", ifn])


def apply_openvpn(conn: sqlite3.Connection, link: Dict[str, Any]) -> None:
    d = _vpn_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    cfg = link.get("config") or {}
    remote = (cfg.get("remote") or link.get("endpoint") or "").strip()
    if ":" in remote:
        host, port_s = remote.rsplit(":", 1)
        port = port_s.strip()
        remote_line = f"remote {host.strip()} {port}"
    else:
        remote_line = f"remote {remote} 1194" if remote else ""
    if not remote_line:
        storage.set_vpn_link_status(conn, int(link["id"]), actual_status="down", last_error="openvpn_missing_remote")
        storage.append_vpn_event_log(conn, "vpn", int(link["id"]), "OpenVPN: missing remote")
        return

    ca = (cfg.get("ca") or "").strip()
    cert = (cfg.get("cert") or "").strip()
    key = (cfg.get("key") or "").strip()
    proto = (cfg.get("proto") or "udp").strip()
    ifn = (link.get("iface_name") or f"mtrt{link['id']}").strip()[:15]
    vrf = storage.validate_vrf_name(link.get("vrf") or "vrf2103")

    up_script = d / "mtr-openvpn-up.sh"
    up_script.write_text(
        "#!/bin/sh\n"
        f'VRF="{vrf}"\n'
        'if [ -n "$dev" ]; then ip link set "$dev" master "$VRF" 2>/dev/null || true; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    up_script.chmod(0o755)

    conf = d / f"openvpn-{link['id']}.conf"
    lines = [
        "client",
        "dev-type tun",
        f"dev {ifn}",
        proto,
        remote_line,
        "persist-tun",
        "script-security 2",
        f"up {up_script}",
        "writepid " + str(d / f"openvpn-{link['id']}.pid"),
        "verb 3",
    ]
    if ca:
        lines.append(f"ca {ca}")
    if cert:
        lines.append(f"cert {cert}")
    if key:
        lines.append(f"key {key}")
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    teardown_openvpn(link)
    openvpn_bin = shutil.which("openvpn") or "/usr/sbin/openvpn"
    code, out, err = _run(
        _sudo_prefix() + [openvpn_bin, "--config", str(conf), "--daemon", "--cd", str(d)],
        timeout=30,
    )
    if code != 0:
        storage.set_vpn_link_status(conn, int(link["id"]), actual_status="down", last_error=f"openvpn_start: {err or out}")
        storage.append_vpn_event_log(conn, "vpn", int(link["id"]), f"OpenVPN start failed: {err or out}")
        return
    storage.set_vpn_link_status(conn, int(link["id"]), actual_status="up", last_error="")
    storage.append_vpn_event_log(conn, "vpn", int(link["id"]), f"OpenVPN daemon started config={conf}")


def _l2tp_bundle_dir(link_id: int) -> Path:
    return _vpn_data_dir() / f"l2tp-{link_id}"


def teardown_l2tp(link: Dict[str, Any]) -> None:
    """删除本服务生成的 L2TP 配置包（不停止全局 xl2tpd/strongSwan）。"""
    d = _l2tp_bundle_dir(int(link["id"]))
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)


def apply_l2tp(conn: sqlite3.Connection, link: Dict[str, Any]) -> None:
    """
    L2TP/IPsec：在 MTR_OP_DATA/vpn/l2tp-<id>/ 生成本机可合并的配置片段 + ppp ip-up 绑 vrf2103。
    默认**不**自动重启系统 xl2tpd/ipsec（避免与现网全局配置冲突）；设置 MTR_L2TP_APPLY=1 时尝试
    `ipsec rereadsecrets` + `ipsec stroke up <conn>`（strongSwan 5.x）及 `xl2tpd` reload（若存在 unit）。
    """
    cfg = (link.get("config") or {}).get("l2tp") or {}
    server = (cfg.get("server") or (link.get("endpoint") or "").strip()).split(":")[0].strip()
    username = (cfg.get("username") or "").strip()
    psk = (cfg.get("ipsec_psk") or cfg.get("psk") or "").strip()
    password = (cfg.get("password") or "").strip()
    pw_file = (cfg.get("password_file") or "").strip()
    if not password and pw_file:
        p = Path(pw_file)
        if not p.is_file():
            storage.set_vpn_link_status(conn, int(link["id"]), actual_status="down", last_error="l2tp_password_file_missing")
            storage.append_vpn_event_log(conn, "vpn", int(link["id"]), f"L2TP: password_file 不存在: {pw_file}")
            return
        password = p.read_text(encoding="utf-8", errors="replace").strip().split("\n")[0]

    if not password:
        storage.set_vpn_link_status(conn, int(link["id"]), actual_status="down", last_error="l2tp_missing_password")
        storage.append_vpn_event_log(conn, "vpn", int(link["id"]), "L2TP: 提供 config.l2tp.password 或 password_file")
        return

    if not server or not username:
        storage.set_vpn_link_status(
            conn,
            int(link["id"]),
            actual_status="down",
            last_error="l2tp_missing_server_or_username",
        )
        storage.append_vpn_event_log(conn, "vpn", int(link["id"]), "L2TP: config.l2tp.server / username 或 endpoint+username")
        return
    if not psk:
        storage.set_vpn_link_status(conn, int(link["id"]), actual_status="down", last_error="l2tp_missing_ipsec_psk")
        storage.append_vpn_event_log(conn, "vpn", int(link["id"]), "L2TP: 请在 config.l2tp.ipsec_psk 提供 IPsec PSK")
        return

    vrf = storage.validate_vrf_name(link.get("vrf") or "vrf2103")
    lac_name = (cfg.get("lac_name") or f"mtr-l2tp-{link['id']}").strip().replace(" ", "_")[:32]

    d = _l2tp_bundle_dir(int(link["id"]))
    d.mkdir(parents=True, exist_ok=True)

    # PPP：拨号成功后把接口并入 VRF（由 xl2tpd 调用 pppd options 中的 ip-up）
    ip_up = d / "ip-up-vrf.sh"
    ip_up.write_text(
        "#!/bin/sh\n"
        "# pppd ip-up: $1=iface $6=tty — 将 ppp 口并入 L2TP 业务 VRF\n"
        f'VRF="{vrf}"\n'
        'IFACE="${1:-}"\n'
        'if [ -n "$IFACE" ]; then ip link set "$IFACE" master "$VRF" 2>/dev/null || true; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    ip_up.chmod(ip_up.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    ppp_opts = d / "options.xl2tpd.client"
    pw_esc = password.replace("\\", "\\\\").replace('"', '\\"')
    pwd_line = f'password "{pw_esc}"\n'
    ppp_opts.write_text(
        "ipcp-accept-local\nipcp-accept-remote\n"
        "refuse-eap\nrefuse-pap\nrefuse-chap\nrefuse-mschap\n"
        "require-mschap-v2\nnoccp\n"
        "mtu 1410\nmru 1410\n"
        f"ipparam {lac_name}\n"
        f"name {username}\n"
        f"{pwd_line}"
        f"ip-up {ip_up.resolve()}\n",
        encoding="utf-8",
    )
    if password:
        ppp_opts.chmod(stat.S_IRUSR | stat.S_IWUSR)

    xl2 = d / "xl2tpd-lac.conf"
    xl2.write_text(
        f"; 合并到系统 /etc/xl2tpd/xl2tpd.conf 或 include 本文件（路径 {xl2.resolve()}）\n"
        f"[lac {lac_name}]\n"
        f"lns = {server}\n"
        "ppp debug = no\n"
        f"pppoptfile = {ppp_opts.resolve()}\n"
        "autodial = yes\n"
        "redial = yes\n"
        "redial timeout = 15\n",
        encoding="utf-8",
    )

    conn_id = f"mtr-l2tp-{link['id']}"
    ipsec_conf = d / "ipsec.conf.snippet"
    ipsec_conf.write_text(
        f"# 合并到 /etc/ipsec.conf 或 swanctl 等价配置；连接名 {conn_id}\n"
        f"conn {conn_id}\n"
        "  keyexchange=ikev1\n"
        "  authby=secret\n"
        "  type=transport\n"
        "  left=%defaultroute\n"
        "  leftprotoport=17/1701\n"
        f"  right={server}\n"
        "  rightprotoport=17/1701\n"
        "  auto=add\n"
        "  ike=aes128-sha1-modp2048!\n"
        "  esp=aes128-sha1!\n\n",
        encoding="utf-8",
    )
    secrets = d / "ipsec.secrets.snippet"
    secrets.write_text(
        f"# 追加到 /etc/ipsec.secrets 后执行: ipsec rereadsecrets\n"
        f"%any {server} : PSK \"{psk}\"\n",
        encoding="utf-8",
    )
    secrets.chmod(stat.S_IRUSR | stat.S_IWUSR)

    readme = d / "README.txt"
    readme.write_text(
        "L2TP/IPsec 配置包（由 mtr_op 生成）\n"
        "--------------------------------\n"
        f"1. 将 xl2tpd-lac.conf 内容并入本机 xl2tpd（或 include 该文件）。\n"
        f"2. 将 ipsec.conf.snippet 并入 strongSwan/Libreswan 的 ipsec.conf；ipsec.secrets.snippet 并入 ipsec.secrets。\n"
        "3. 执行: ipsec restart 或 ipsec stroke reload；再 systemctl restart xl2tpd（发行版命令可能不同）。\n"
        f"4. 拨号成功后 ppp 口应执行 ip-up-vrf.sh，将接口并入 {vrf}。\n"
        "5. 密钥与密码仅在本目录，请限制权限并勿提交版本库。\n",
        encoding="utf-8",
    )

    storage.set_vpn_link_status(
        conn,
        int(link["id"]),
        actual_status="down",
        last_error=f"l2tp_bundle_ready:{d}",
    )
    storage.append_vpn_event_log(
        conn,
        "vpn",
        int(link["id"]),
        f"L2TP 配置包已写入 {d}；按需合并到系统 xl2tpd/ipsec 后拨号；可选 MTR_L2TP_APPLY=1 尝试 stroke",
    )

    if os.environ.get("MTR_L2TP_APPLY", "").strip().lower() not in {"1", "true", "yes"}:
        return

    # best-effort：strongSwan classic ipsec stroke（失败则仅保留配置包）
    for cmd in (
        _sudo_prefix() + ["ipsec", "rereadsecrets"],
        _sudo_prefix() + ["ipsec", "stroke", "up", conn_id],
    ):
        code, out, err = _run(cmd, timeout=45)
        if code != 0:
            storage.append_vpn_event_log(conn, "vpn", int(link["id"]), f"L2TP stroke: {' '.join(cmd)} -> {err or out}")
    xl2tpd = shutil.which("xl2tpd")
    if shutil.which("systemctl"):
        _run(_sudo_prefix() + ["systemctl", "try-reload-or-restart", "xl2tpd"], timeout=30)
    elif xl2tpd:
        _run(_sudo_prefix() + ["killall", "-HUP", "xl2tpd"], timeout=10)


def apply_all(conn: sqlite3.Connection) -> Dict[str, Any]:
    """幂等：先拆策略与隧道，再按库重建。"""
    if not vpn_apply_enabled():
        storage.append_vpn_event_log(conn, "vpn_apply", None, "skipped: non-Linux or MTR_OP_VPN_APPLY=0")
        return {"ok": True, "skipped": True}

    policies = storage.list_vpn_policies(conn)
    for pol in policies:
        _teardown_policy_kernel(pol)

    links = storage.list_vpn_links(conn)
    for link in links:
        if link["link_type"] == "gre":
            teardown_gre(link)
        elif link["link_type"] == "openvpn":
            teardown_openvpn(link)
        elif link["link_type"] == "l2tp":
            teardown_l2tp(link)

    links_by_id: Dict[int, Dict[str, Any]] = {}
    for link in sorted(links, key=lambda x: (int(x["priority"]), int(x["id"]))):
        links_by_id[int(link["id"])] = link
        if not link.get("enabled") or not link.get("desired_up"):
            storage.set_vpn_link_status(conn, int(link["id"]), actual_status="disabled", last_error="")
            continue
        if link["link_type"] == "gre":
            apply_gre(conn, link)
        elif link["link_type"] == "openvpn":
            apply_openvpn(conn, link)
        elif link["link_type"] == "l2tp":
            apply_l2tp(conn, link)
        refresh_link_stats(conn, link)

    # 重建策略（仅 enabled）
    links2 = storage.list_vpn_links(conn)
    links_by_id = {int(x["id"]): x for x in links2}
    for pol in storage.list_vpn_policies(conn):
        if pol.get("enabled"):
            apply_policy(conn, pol, links_by_id)

    storage.append_vpn_event_log(conn, "vpn_apply", None, "apply_all completed")
    return {"ok": True, "skipped": False, "links": len(links2), "policies": len(policies)}


def reconcile_status(conn: sqlite3.Connection) -> None:
    """根据接口存在性刷新 actual_status（轻量）。"""
    for link in storage.list_vpn_links(conn):
        if not link.get("enabled") or not link.get("desired_up"):
            continue
        err = link.get("last_error") or ""
        if link["link_type"] == "l2tp" and (
            err.startswith("l2tp_missing_") or err.startswith("l2tp_password_file")
        ):
            continue
        if _tunnel_up(conn, link):
            storage.set_vpn_link_status(conn, int(link["id"]), actual_status="up")
        else:
            storage.set_vpn_link_status(conn, int(link["id"]), actual_status="down")
        refresh_link_stats(conn, link)
