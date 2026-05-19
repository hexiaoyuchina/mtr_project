"""hop_replace_rules 变更后：重写 /tmp/mtr_te_map.env 并重启 te_rewrite_nfqueue.py。

实验室路径：脚本与 uvicorn 同部署根目录（…/te_rewrite_nfqueue.py）。
可通过 MTR_TE_REWRITE_SKIP_SYNC=1 关闭；MTR_TE_REWRITE_SCRIPT 覆盖脚本路径。
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import storage
from .hop_cidr import iter_ipv4_addresses
from .te_rewrite_peer_sync import sync_te_rewrite_peers

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
TE_MAP_FILE = Path(os.environ.get("MTR_TE_REWRITE_MAP_FILE", "/tmp/mtr_te_map.env"))
TE_LOG_FILE = Path(os.environ.get("MTR_TE_REWRITE_LOG", "/tmp/te_rewrite_nfqueue.log"))
TE_QUEUE_NUM = (os.environ.get("MTR_TE_QUEUE_NUM") or "1").strip() or "1"
# 实验室旧默认口名；换机部署后 sync 会一并 -D 掉残留规则
_LEGACY_DOWNSTREAM_IFACE = "ens192"
_LEGACY_UPLINK_IFACE = "ens224"
TE_PROBE_SRC = (os.environ.get("MTR_TE_PROBE_SRC") or "10.133.152.204").strip()
TE_RETURN_IP = (os.environ.get("MTR_TE_RETURN_IP") or "10.133.152.200").strip()


def _downstream_iface() -> str:
    """TE 出下游口（mangle -o）：优先 MTR_TE_REWRITE_OIF，否则卫星/下联父口。"""
    for key in (
        "MTR_TE_REWRITE_OIF",
        "MTR_BGP_IPVLAN_BASE_IFACE",
        "MTR_OP_DOWNSTREAM_IFACE",
    ):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    return _LEGACY_DOWNSTREAM_IFACE


def _uplink_iface() -> str:
    """TE 自上联入（mangle -i）：优先 MTR_TE_REWRITE_IIF，否则 RR 上联口。"""
    for key in ("MTR_TE_REWRITE_IIF", "MTR_BGP_RR_UPLINK_IFACE"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    return _LEGACY_UPLINK_IFACE


def _nfqueue_forward_specs_active() -> list[list[str]]:
    """当前应安装的 FORWARD NFQUEUE 匹配（下联出 + 上联入下联出）。"""
    oif = _downstream_iface()
    iif = _uplink_iface()
    return [["-o", oif], ["-i", iif, "-o", oif]]


def _nfqueue_forward_specs_all_for_removal() -> list[list[str]]:
    """删除时用：当前口 + 实验室旧口，避免换机后 ens192/ens224 残留。"""
    oifs: list[str] = []
    iifs: list[str] = []
    for v in (_downstream_iface(), _LEGACY_DOWNSTREAM_IFACE):
        if v and v not in oifs:
            oifs.append(v)
    for v in (_uplink_iface(), _LEGACY_UPLINK_IFACE):
        if v and v not in iifs:
            iifs.append(v)
    specs: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for oif in oifs:
        s = ["-o", oif]
        t = tuple(s)
        if t not in seen:
            seen.add(t)
            specs.append(s)
    for iif in iifs:
        for oif in oifs:
            s = ["-i", iif, "-o", oif]
            t = tuple(s)
            if t not in seen:
                seen.add(t)
                specs.append(s)
    return specs


def _script_path() -> Path:
    raw = (os.environ.get("MTR_TE_REWRITE_SCRIPT") or "").strip()
    if raw:
        return Path(raw)
    return ROOT / "te_rewrite_nfqueue.py"


def _python_for_te_daemon() -> str:
    """OP API 常在 venv 内运行；venv 往往未装 NetfilterQueue，不可用 sys.executable 拉 TE 守护进程。"""
    raw = (os.environ.get("MTR_TE_REWRITE_PYTHON") or "").strip()
    if raw:
        return raw
    for cand in ("/usr/bin/python3", shutil.which("python3") or ""):
        if cand and Path(cand).is_file():
            return cand
    return sys.executable


def build_rewrite_map_line(conn: sqlite3.Connection) -> str:
    """按 hop 规则生成 TE 改写映射；网段按与 mtr_spoof 相同的「起始 IP + 前缀」展开为多个 host。"""
    max_expand = int(os.environ.get("MTR_TE_REWRITE_MAX_EXPAND", "4096"))
    seen: set[str] = set()
    parts: list[str] = []
    for row in storage.list_hop_rules_enabled(conn):
        mc = (row.match_cidr or "").strip()
        fg = (row.forged_src or "").strip()
        if not mc or not fg:
            continue
        try:
            for addr in iter_ipv4_addresses(mc, max_addresses=max_expand):
                s = str(addr)
                if s in seen:
                    continue
                seen.add(s)
                parts.append(f"{s}={fg}")
        except ValueError as e:
            logger.warning(
                "te_rewrite map skip rule id=%s match=%s: %s",
                row.id,
                mc,
                e,
            )
            continue
    return ",".join(parts)


def write_te_map_env(line: str) -> None:
    TE_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TE_MAP_FILE.open("w", encoding="ascii", errors="replace") as f:
        f.write("export MTR_TE_REWRITE_MAP=" + repr(line) + "\n")


def _iptables_del_rule(argv: list[str]) -> None:
    subprocess.run(argv, capture_output=True, text=True)


def clear_iptables_nfqueue() -> None:
    """移除 FORWARD NFQUEUE，避免无守护进程时丢包。"""
    _iptables_nfqueue_remove_specs(_nfqueue_forward_specs_all_for_removal())


def _iptables_nfqueue_remove_specs(specs: list[list[str]]) -> None:
    base_rm = [
        "iptables",
        "-t",
        "mangle",
        "-D",
        "FORWARD",
        "-p",
        "icmp",
        "-m",
        "icmp",
        "--icmp-type",
        "time-exceeded",
    ]
    for spec in specs:
        # 重复 -D 直到删净（历史上可能多次插入）
        for _ in range(8):
            r = subprocess.run(
                base_rm + spec + ["-j", "NFQUEUE", "--queue-num", TE_QUEUE_NUM],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                break


def ensure_iptables_nfqueue() -> None:
    """mangle FORWARD：转发的 ICMP TE（上联→下联回程等）进 NFQUEUE。"""
    _iptables_nfqueue_remove_specs(_nfqueue_forward_specs_all_for_removal())
    specs = _nfqueue_forward_specs_active()
    for spec in reversed(specs):
        ins = [
            "iptables",
            "-t",
            "mangle",
            "-I",
            "FORWARD",
            "1",
            "-p",
            "icmp",
            "-m",
            "icmp",
            "--icmp-type",
            "time-exceeded",
            *spec,
            "-j",
            "NFQUEUE",
            "--queue-num",
            TE_QUEUE_NUM,
        ]
        r = subprocess.run(ins, capture_output=True, text=True)
        if r.returncode != 0:
            logger.warning(
                "iptables mangle NFQUEUE %s: %s",
                " ".join(spec),
                (r.stderr or r.stdout or "").strip(),
            )
    logger.info(
        "iptables mangle NFQUEUE: oif=%s iif=%s queue=%s",
        _downstream_iface(),
        _uplink_iface(),
        TE_QUEUE_NUM,
    )


def clear_probe_return_via_200() -> None:
    """移除探测 SNAT/DNAT（避免邻近跳对 152.200 回包失败导致 mtr 第 2 跳 ???）。"""
    if not TE_PROBE_SRC or not TE_RETURN_IP:
        return
    snat = [
        "iptables",
        "-t",
        "nat",
        "-D",
        "POSTROUTING",
        "-s",
        TE_PROBE_SRC,
        "-o",
        _uplink_iface(),
        "-j",
        "SNAT",
        "--to-source",
        TE_RETURN_IP,
    ]
    dnat = [
        "iptables",
        "-t",
        "nat",
        "-D",
        "PREROUTING",
        "-p",
        "icmp",
        "-m",
        "icmp",
        "--icmp-type",
        "time-exceeded",
        "-d",
        TE_RETURN_IP,
        "-j",
        "DNAT",
        "--to-destination",
        TE_PROBE_SRC,
    ]
    _iptables_del_rule(snat)
    _iptables_del_rule(dnat)


def ensure_probe_return_via_200() -> None:
    """可选：出网 SNAT 为 200、回程 TE DNAT 回 201。默认关闭（邻近设备往往无法回 152.200）。

    无 SNAT 时，经 200 转发的 TE（目的 152.204）仍由 FORWARD NFQUEUE 改写。
    若需强制全路径经 200，须在 ROS/邻近跳配置到 152.200 的路由后再开启。
    """
    if os.environ.get("MTR_TE_PROBE_RETURN_VIA_200", "0").strip().lower() in {
        "0",
        "false",
        "no",
    }:
        clear_probe_return_via_200()
        return
    if not TE_PROBE_SRC or not TE_RETURN_IP:
        return
    snat = [
        "iptables",
        "-t",
        "nat",
        "-D",
        "POSTROUTING",
        "-s",
        TE_PROBE_SRC,
        "-o",
        _uplink_iface(),
        "-j",
        "SNAT",
        "--to-source",
        TE_RETURN_IP,
    ]
    dnat = [
        "iptables",
        "-t",
        "nat",
        "-D",
        "PREROUTING",
        "-p",
        "icmp",
        "-m",
        "icmp",
        "--icmp-type",
        "time-exceeded",
        "-d",
        TE_RETURN_IP,
        "-j",
        "DNAT",
        "--to-destination",
        TE_PROBE_SRC,
    ]
    _iptables_del_rule(snat)
    _iptables_del_rule(dnat)
    upl = _uplink_iface()
    r1 = subprocess.run(
        [
            "iptables",
            "-t",
            "nat",
            "-A",
            "POSTROUTING",
            "-s",
            TE_PROBE_SRC,
            "-o",
            upl,
            "-j",
            "SNAT",
            "--to-source",
            TE_RETURN_IP,
        ],
        capture_output=True,
        text=True,
    )
    r2 = subprocess.run(
        [
            "iptables",
            "-t",
            "nat",
            "-A",
            "PREROUTING",
            "-p",
            "icmp",
            "-m",
            "icmp",
            "--icmp-type",
            "time-exceeded",
            "-d",
            TE_RETURN_IP,
            "-j",
            "DNAT",
            "--to-destination",
            TE_PROBE_SRC,
        ],
        capture_output=True,
        text=True,
    )
    if r1.returncode != 0 or r2.returncode != 0:
        logger.warning(
            "probe return via 200 nat: snat=%s dnat=%s",
            (r1.stderr or r1.stdout or "").strip(),
            (r2.stderr or r2.stdout or "").strip(),
        )
    else:
        logger.info(
            "probe return via 200: %s -> SNAT %s on %s, TE DNAT back to %s",
            TE_PROBE_SRC,
            TE_RETURN_IP,
            upl,
            TE_PROBE_SRC,
        )


def restart_te_rewrite_daemon(map_line: str) -> None:
    script = _script_path()
    subprocess.run(["pkill", "-f", "te_rewrite_nfqueue.py"], capture_output=True, text=True)
    env = os.environ.copy()
    env["MTR_TE_REWRITE_MAP"] = map_line
    env["MTR_TE_QUEUE_NUM"] = TE_QUEUE_NUM
    TE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    py_exe = _python_for_te_daemon()
    exe = shlex.quote(py_exe)
    sp = shlex.quote(str(script))
    lg = shlex.quote(str(TE_LOG_FILE))
    bash_cmd = f"nohup {exe} {sp} >> {lg} 2>&1 &"
    logger.info("te_rewrite_sync: restart daemon python=%s script=%s", py_exe, script)
    try:
        subprocess.run(["bash", "-c", bash_cmd], env=env, timeout=15)
    except OSError as e:
        logger.warning("te_rewrite_nfqueue start failed: %s", e)


def sync_te_rewrite_from_conn(conn: sqlite3.Connection) -> None:
    if os.environ.get("MTR_TE_REWRITE_SKIP_SYNC", "").strip().lower() in {"1", "true", "yes"}:
        return
    # 每次同步先清实验室旧口残留（即使 hijack 关闭也执行）
    _iptables_nfqueue_remove_specs(_nfqueue_forward_specs_all_for_removal())
    script = _script_path()
    if not script.is_file():
        logger.debug("te_rewrite_sync: skip (no script %s)", script)
        return
    try:
        subprocess.run(["modprobe", "nfnetlink_queue"], capture_output=True, text=True)
    except OSError:
        pass
    g = storage.get_global(conn)
    if not g.hijack_enabled:
        line = ""
        logger.info(
            "te_rewrite_sync: hijack_enabled=false, TE map cleared (iptables NFQUEUE pass-through)"
        )
    else:
        line = build_rewrite_map_line(conn)
    write_te_map_env(line)
    if g.hijack_enabled and line:
        ensure_iptables_nfqueue()
        restart_te_rewrite_daemon(line)
    else:
        clear_iptables_nfqueue()
        subprocess.run(
            ["pkill", "-f", "te_rewrite_nfqueue.py"],
            capture_output=True,
            text=True,
        )
    ensure_probe_return_via_200()
    try:
        sync_te_rewrite_peers(line, local_script=script)
    except Exception:
        logger.exception("te_rewrite_peer_sync failed")
    logger.info(
        "te_rewrite_sync: map_len=%s (empty=pass-through TE) script=%s",
        len(line),
        script,
    )
