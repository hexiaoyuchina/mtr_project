"""hop_replace_rules 变更后：原子写 /tmp/mtr_te_map.env，优先 SIGHUP 热加载（不 pkill）。

仅总开关切换/启动时做 iptables 全量整理；日常增删改规则尽量缩短 NFQUEUE 空窗。
可通过 MTR_TE_REWRITE_SKIP_SYNC=1 关闭；MTR_TE_REWRITE_SCRIPT 覆盖脚本路径。
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
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


def _te_rewrite_output_enabled() -> bool:
    """本机生成的 ICMP TE 走 OUTPUT；默认开启（MTR_TE_REWRITE_OUTPUT=0 可关）。"""
    return os.environ.get("MTR_TE_REWRITE_OUTPUT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _nfqueue_oif_specs_for_removal() -> list[list[str]]:
    """按出接口列举 -o 匹配（FORWARD/OUTPUT 删除残留共用）。"""
    oifs: list[str] = []
    for v in (_downstream_iface(), _uplink_iface(), _LEGACY_DOWNSTREAM_IFACE, _LEGACY_UPLINK_IFACE):
        if v and v not in oifs:
            oifs.append(v)
    return [["-o", o] for o in oifs]


def _nfqueue_forward_specs_active() -> list[list[str]]:
    """当前应安装的 FORWARD NFQUEUE 匹配（下联出 + 上联入下联出）。"""
    oif = _downstream_iface()
    iif = _uplink_iface()
    return [["-o", oif], ["-i", iif, "-o", oif]]


def _nfqueue_output_specs_active() -> list[list[str]]:
    """本机发出的 TE（如 105.94→公网 第 1 跳 209）：OUTPUT -o 下联/上联。"""
    return _nfqueue_oif_specs_for_removal()


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
    """按 hop 规则生成 TE 改写映射；网段按「起始 IP + 前缀」连续展开为多个 host。"""
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
    """原子写入，供守护进程 SIGHUP 与启动时读取。"""
    TE_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TE_MAP_FILE.with_name(TE_MAP_FILE.name + ".tmp")
    content = "export MTR_TE_REWRITE_MAP=" + repr(line) + "\n"
    tmp.write_text(content, encoding="ascii", errors="replace")
    tmp.replace(TE_MAP_FILE)


def _te_daemon_pids() -> list[int]:
    r = subprocess.run(
        ["pgrep", "-f", "te_rewrite_nfqueue.py"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return []
    out: list[int] = []
    for part in (r.stdout or "").split():
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


def _nfqueue_has_listener(queue_num: str | None = None) -> bool:
    """内核已登记 NFQUEUE 监听（/proc/net/netfilter/nfnetlink_queue 非空且含队列号）。"""
    q = (queue_num or TE_QUEUE_NUM).strip()
    p = Path("/proc/net/netfilter/nfnetlink_queue")
    try:
        raw = p.read_text(encoding="ascii", errors="replace")
    except OSError:
        return False
    if not raw.strip():
        return False
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == q:
            try:
                return int(parts[1]) > 0
            except ValueError:
                return True
    return True


def _wait_te_daemon_bound(timeout_sec: float = 45.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _nfqueue_has_listener():
            return True
        time.sleep(0.15)
    return False


def _stop_te_rewrite_force() -> None:
    subprocess.run(
        ["pkill", "-9", "-f", "te_rewrite_nfqueue.py"],
        capture_output=True,
        text=True,
    )
    time.sleep(0.25)


def _sighup_reload_reflected_in_log(map_line: str) -> bool:
    """确认最近一次 reload 日志含 map 中首个 forged（防旧 env 盖住文件）。"""
    probe = (map_line or "").strip()
    if not probe:
        return True
    first = probe.split(",", 1)[0].strip()
    if "=" not in first:
        return True
    forged = first.split("=", 1)[1].strip()
    if not forged:
        return True
    try:
        text = TE_LOG_FILE.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return True
    if "reload rules=" not in text:
        return False
    last = text.rsplit("reload rules=", 1)[-1].split("\n", 1)[0]
    return forged in last


def reload_te_rewrite_daemon(map_line: str = "") -> bool:
    """向运行中守护进程发 SIGHUP，从 MTR_TE_REWRITE_MAP_FILE 热加载。

    若进程不支持 SIGHUP、reload 未反映到日志、或进程退出，返回 False 以便走冷启动。
    """
    pids = _te_daemon_pids()
    if not pids:
        return False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGHUP)
        except ProcessLookupError:
            continue
        except OSError as e:
            logger.warning("te_rewrite SIGHUP pid=%s: %s", pid, e)
    time.sleep(0.15)
    alive = _te_daemon_pids()
    if not alive:
        logger.warning("te_rewrite_sync: SIGHUP后无存活进程，将冷启动")
        return False
    if not _sighup_reload_reflected_in_log(map_line):
        logger.warning(
            "te_rewrite_sync: SIGHUP 后日志未含期望 forged=%s，将冷启动",
            (map_line or "").split(",", 1)[0],
        )
        return False
    logger.info("te_rewrite_sync: SIGHUP reload ok pids=%s", alive)
    return True


def _iptables_del_rule(argv: list[str]) -> None:
    subprocess.run(argv, capture_output=True, text=True)


def clear_iptables_nfqueue() -> None:
    """移除 FORWARD/OUTPUT NFQUEUE，避免无守护进程时丢包。"""
    _iptables_nfqueue_remove_chain("FORWARD", _nfqueue_forward_specs_all_for_removal())
    _iptables_nfqueue_remove_chain("OUTPUT", _nfqueue_oif_specs_for_removal())


def _iptables_nfqueue_remove_chain(chain: str, specs: list[list[str]]) -> None:
    """删除须与安装一致用 icmp type 11；并尝试 time-exceeded 以清历史残留。"""
    for icmp_t in ("11", "time-exceeded"):
        base_rm = [
            "iptables",
            "-t",
            "mangle",
            "-D",
            chain,
            "-p",
            "icmp",
            "-m",
            "icmp",
            "--icmp-type",
            icmp_t,
        ]
        for spec in specs:
            for _ in range(8):
                r = subprocess.run(
                    base_rm + spec + ["-j", "NFQUEUE", "--queue-num", TE_QUEUE_NUM],
                    capture_output=True,
                    text=True,
                )
                if r.returncode != 0:
                    break


def _iptables_nfqueue_present(chain: str, spec: list[str]) -> bool:
    r = subprocess.run(
        [
            "iptables",
            "-t",
            "mangle",
            "-C",
            chain,
            "-p",
            "icmp",
            "-m",
            "icmp",
            "--icmp-type",
            "11",
            *spec,
            "-j",
            "NFQUEUE",
            "--queue-num",
            TE_QUEUE_NUM,
        ],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def _iptables_nfqueue_install_chain(chain: str, specs: list[list[str]]) -> None:
    for spec in reversed(specs):
        ins = [
            "iptables",
            "-t",
            "mangle",
            "-I",
            chain,
            "1",
            "-p",
            "icmp",
            "-m",
            "icmp",
            "--icmp-type",
            "11",
            *spec,
            "-j",
            "NFQUEUE",
            "--queue-num",
            TE_QUEUE_NUM,
        ]
        r = subprocess.run(ins, capture_output=True, text=True)
        if r.returncode != 0:
            logger.warning(
                "iptables mangle %s NFQUEUE %s: %s",
                chain,
                " ".join(spec),
                (r.stderr or r.stdout or "").strip(),
            )


def ensure_iptables_nfqueue(*, flush_legacy: bool = False) -> None:
    """mangle FORWARD/OUTPUT：ICMP TE 进 NFQUEUE。flush_legacy 仅启动/总开关时清旧口残留。"""
    if flush_legacy:
        _iptables_nfqueue_remove_chain("FORWARD", _nfqueue_forward_specs_all_for_removal())
        _iptables_nfqueue_remove_chain("OUTPUT", _nfqueue_oif_specs_for_removal())
    missing_fwd = [
        s
        for s in _nfqueue_forward_specs_active()
        if not _iptables_nfqueue_present("FORWARD", s)
    ]
    if missing_fwd:
        _iptables_nfqueue_install_chain("FORWARD", missing_fwd)
    if _te_rewrite_output_enabled():
        missing_out = [
            s
            for s in _nfqueue_output_specs_active()
            if not _iptables_nfqueue_present("OUTPUT", s)
        ]
        if missing_out:
            _iptables_nfqueue_install_chain("OUTPUT", missing_out)
    logger.info(
        "iptables mangle NFQUEUE: forward oif=%s iif=%s output=%s queue=%s flush_legacy=%s",
        _downstream_iface(),
        _uplink_iface(),
        _te_rewrite_output_enabled(),
        TE_QUEUE_NUM,
        flush_legacy,
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


def start_te_rewrite_daemon(map_line: str) -> None:
    """冷启动 te_rewrite；失败则拆除 NFQUEUE，避免 TE 被丢导致 mtr 超时。"""
    _stop_te_rewrite_force()
    script = _script_path()
    env = os.environ.copy()
    env["MTR_TE_REWRITE_MAP"] = map_line
    env["MTR_TE_REWRITE_MAP_FILE"] = str(TE_MAP_FILE)
    env["MTR_TE_QUEUE_NUM"] = TE_QUEUE_NUM
    TE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    py_exe = _python_for_te_daemon()
    logger.info("te_rewrite_sync: start daemon python=%s script=%s", py_exe, script)
    try:
        with TE_LOG_FILE.open("a", encoding="utf-8", errors="replace") as logf:
            subprocess.Popen(
                [py_exe, str(script)],
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as e:
        logger.warning("te_rewrite_nfqueue start failed: %s", e)
        return
    if not _wait_te_daemon_bound(90.0):
        tail = ""
        try:
            tail = TE_LOG_FILE.read_text(encoding="utf-8", errors="replace")[-800:]
        except OSError:
            pass
        logger.error(
            "te_rewrite_nfqueue did not bind NFQUEUE queue=%s within 45s; "
            "clearing iptables NFQUEUE (TE pass-through in kernel). log_tail=%s",
            TE_QUEUE_NUM,
            tail,
        )
        _stop_te_rewrite_force()
        clear_iptables_nfqueue()


def _apply_te_rewrite_runtime(
    *,
    line: str,
    hijack_enabled: bool,
    flush_iptables_legacy: bool,
) -> None:
    script = _script_path()
    if not script.is_file():
        logger.debug("te_rewrite_sync: skip (no script %s)", script)
        return
    write_te_map_env(line)
    if hijack_enabled:
        # 须先 bind NFQUEUE 再装 iptables，否则 TE 进无人队列会被内核丢弃（mtr 全 timeout）。
        clear_iptables_nfqueue()
        start_te_rewrite_daemon(line)
        if _nfqueue_has_listener():
            ensure_iptables_nfqueue(flush_legacy=flush_iptables_legacy)
        else:
            logger.error(
                "te_rewrite_sync: NFQUEUE not bound; iptables left clear (TE pass-through)"
            )
    else:
        clear_iptables_nfqueue()
        subprocess.run(
            ["pkill", "-f", "te_rewrite_nfqueue.py"],
            capture_output=True,
            text=True,
        )


def _kill_legacy_mtr_spoof() -> None:
    """移除已废弃的路径 B（Echo→NFQUEUE 合成 TE），避免与 te_rewrite 抢同一队列。"""
    subprocess.run(
        ["pkill", "-f", "mtr_spoof_nfqueue"],
        capture_output=True,
        text=True,
    )


def sync_te_rewrite_from_conn(
    conn: sqlite3.Connection,
    *,
    flush_iptables_legacy: bool = False,
) -> None:
    """日常 hop 规则变更：默认 SIGHUP 热加载（不 pkill）；lifespan / 总开关才冷启动。"""
    if os.environ.get("MTR_TE_REWRITE_SKIP_SYNC", "").strip().lower() in {"1", "true", "yes"}:
        return
    _kill_legacy_mtr_spoof()
    script = _script_path()
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
        _apply_te_rewrite_runtime(
            line=line,
            hijack_enabled=False,
            flush_iptables_legacy=flush_iptables_legacy,
        )
    else:
        line = build_rewrite_map_line(conn)
        write_te_map_env(line)
        if not flush_iptables_legacy and reload_te_rewrite_daemon(line):
            if _nfqueue_has_listener():
                ensure_iptables_nfqueue(flush_legacy=False)
                logger.info("te_rewrite_sync: hot reload only (no pkill), map_len=%s", len(line))
            else:
                logger.warning("te_rewrite_sync: SIGHUP ok but queue unbound; cold start")
                _apply_te_rewrite_runtime(
                    line=line,
                    hijack_enabled=True,
                    flush_iptables_legacy=False,
                )
        else:
            if not flush_iptables_legacy:
                logger.info("te_rewrite_sync: hot reload unavailable; cold start")
            _apply_te_rewrite_runtime(
                line=line,
                hijack_enabled=True,
                flush_iptables_legacy=flush_iptables_legacy,
            )
    ensure_probe_return_via_200()
    try:
        sync_te_rewrite_peers(line, local_script=script)
    except Exception:
        logger.exception("te_rewrite_peer_sync failed")
    logger.info(
        "te_rewrite_sync: map_len=%s hot=%s script=%s",
        len(line),
        not flush_iptables_legacy,
        script,
    )
