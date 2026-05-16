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

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
TE_MAP_FILE = Path(os.environ.get("MTR_TE_REWRITE_MAP_FILE", "/tmp/mtr_te_map.env"))
TE_LOG_FILE = Path(os.environ.get("MTR_TE_REWRITE_LOG", "/tmp/te_rewrite_nfqueue.log"))
TE_QUEUE_NUM = (os.environ.get("MTR_TE_QUEUE_NUM") or "1").strip() or "1"
TE_OIF = (os.environ.get("MTR_TE_REWRITE_OIF") or "ens192").strip() or "ens192"


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


def ensure_iptables_nfqueue() -> None:
    """与 deploy_light 一致：mangle FORWARD icmp type 11 -> NFQUEUE。"""
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
        "-o",
        TE_OIF,
        "-j",
        "NFQUEUE",
        "--queue-num",
        TE_QUEUE_NUM,
    ]
    rm = [
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
        "-o",
        TE_OIF,
        "-j",
        "NFQUEUE",
        "--queue-num",
        TE_QUEUE_NUM,
    ]
    subprocess.run(rm, capture_output=True, text=True)
    r = subprocess.run(ins, capture_output=True, text=True)
    if r.returncode != 0:
        logger.warning("iptables mangle NFQUEUE: %s", (r.stderr or r.stdout or "").strip())


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
    ensure_iptables_nfqueue()
    restart_te_rewrite_daemon(line)
    logger.info(
        "te_rewrite_sync: map_len=%s (empty=pass-through TE) script=%s",
        len(line),
        script,
    )
