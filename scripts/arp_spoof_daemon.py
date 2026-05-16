#!/usr/bin/env python3
"""
Linux 200：ARP 引流（读 OP SQLite）。

全局开关 arp_spoof_settings.arp_spoof_enabled；多条记录见 arp_spoof_targets：
每条指定冒充网关 IPv4、出接口、策略（被动应答过滤）；周期性 GARP 按条发往对应接口。

默认还会在每个 egress_iface 上为 spoof_gateway_ip 添加 /32 主机地址，使本机内核将该 IPv4
视为本机地址，便于对端 ping 通及对 BGP ``update-source`` 生效；逻辑与 OP 进程内
``app.arp_spoof_assign`` 共用（OP 写库后会立即 reconcile，守护进程仍周期性执行）。
可用环境变量 MTR_ARP_ASSIGN_HOST_IP=0 或 --no-assign-host-ip 关闭。

删除 OP 中某条 target 或关闭总开关后，守护进程会对比状态文件并 ``ip addr del`` 撤掉不再需要的 /32，
避免「库记录没了仍能 ping 通」。若本机已安装 **scapy**，撤下后还会按下一跳 MAC 发**恢复 GARP**，
减轻下游（如 Linux 201）``ip neigh`` 仍长期指向本机 MAC 的现象；见 ``app/arp_neighbor_restore.py``。
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DB = Path(__file__).resolve().parent.parent / "service" / "data.db"


def read_iface_mac(iface: str) -> str:
    p = Path("/sys/class/net") / iface / "address"
    return p.read_text(encoding="utf-8").strip().lower()


def parse_victim_nets(policy_cidrs: str) -> List[ipaddress.IPv4Network]:
    out: List[ipaddress.IPv4Network] = []
    for item in (policy_cidrs or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
            if isinstance(net, ipaddress.IPv4Network):
                out.append(net)
        except ValueError:
            continue
    return out


def source_allowed(psrc: str, nets: List[ipaddress.IPv4Network]) -> bool:
    if not nets:
        return True
    try:
        a = ipaddress.ip_address(psrc)
    except ValueError:
        return False
    return any(a in n for n in nets)


def load_settings_row(db_path: Path) -> Optional[sqlite3.Row]:
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT arp_spoof_enabled FROM arp_spoof_settings WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def load_enabled_targets(db_path: Path) -> List[Dict[str, Any]]:
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT spoof_gateway_ip, egress_iface, policy_mode, policy_cidrs, satellite_vrf "
            "FROM arp_spoof_targets WHERE enabled = 1 ORDER BY id ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "spoof_gateway_ip": (r["spoof_gateway_ip"] or "").strip(),
                "egress_iface": (r["egress_iface"] or "").strip(),
                "policy_mode": (r["policy_mode"] or "gateway_only").strip().lower(),
                "policy_cidrs": (r["policy_cidrs"] or "").strip(),
                "satellite_vrf": (r["satellite_vrf"] if "satellite_vrf" in r.keys() else "") or "",
            }
        )
    return [x for x in out if x["spoof_gateway_ip"] and x["egress_iface"]]


def distinct_ifaces(targets: List[Dict[str, Any]]) -> List[str]:
    return sorted({t["egress_iface"] for t in targets})


_ROOT = Path(__file__).resolve().parent
_SERVICE = _ROOT / "service" if (_ROOT / "service").is_dir() else (_ROOT.parent / "service")
if _SERVICE.is_dir():
    _sp = str(_SERVICE.resolve())
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

from app.arp_spoof_assign import reconcile_assigned_host_ips  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="ARP：周期 GARP + 多接口嗅探 who-has")
    parser.add_argument("--op-db", default=os.environ.get("MTR_OP_DB", str(DEFAULT_DB)))
    parser.add_argument(
        "--reload-sec",
        type=float,
        default=float(os.environ.get("MTR_ARP_RELOAD_SEC", "5")),
    )
    parser.add_argument(
        "--garp-interval",
        type=float,
        default=float(os.environ.get("MTR_ARP_GARP_INTERVAL", "10")),
    )
    parser.add_argument(
        "--no-assign-host-ip",
        action="store_true",
        help="不为 spoof_gateway_ip 添加接口 /32（默认添加以便 ping 通该 IP）",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.op_db).expanduser()
    reload_sec = max(1.0, float(args.reload_sec))
    garp_interval = max(1.0, float(args.garp_interval))

    try:
        from scapy.all import ARP, Ether, sendp, sniff  # type: ignore
    except ImportError as e:
        print("需要 scapy：pip install scapy", file=sys.stderr)
        raise SystemExit(1) from e

    stop = threading.Event()

    def send_gratuitous(iface: str, mac: str, ip_s: str) -> None:
        pkt = Ether(dst="ff:ff:ff:ff:ff:ff", src=mac) / ARP(
            op=2,
            hwsrc=mac,
            psrc=ip_s,
            pdst=ip_s,
            hwdst="ff:ff:ff:ff:ff:ff",
        )
        sendp(pkt, iface=iface, verbose=0)

    def garp_loop() -> None:
        while not stop.is_set():
            s = load_settings_row(db_path)
            if s is None or not bool(s["arp_spoof_enabled"]):
                stop.wait(timeout=min(reload_sec, garp_interval))
                continue
            targets = load_enabled_targets(db_path)
            if not targets:
                if args.verbose:
                    print("arp_garp: no enabled targets", flush=True)
                stop.wait(timeout=reload_sec)
                continue
            by_iface: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for t in targets:
                by_iface[t["egress_iface"]].append(t)
            for iface, tlist in by_iface.items():
                try:
                    mac = read_iface_mac(iface)
                except OSError as e:
                    print(f"arp_garp: read MAC failed iface={iface}: {e}", flush=True)
                    continue
                seen_ip: set[str] = set()
                for t in tlist:
                    ip_s = t["spoof_gateway_ip"]
                    if ip_s in seen_ip:
                        continue
                    seen_ip.add(ip_s)
                    send_gratuitous(iface, mac, ip_s)
                    if args.verbose:
                        print(f"arp_garp: GARP {ip_s} -> {mac} via {iface}", flush=True)
            stop.wait(timeout=garp_interval)

    def sniff_loop_iface(iface: str) -> None:
        while not stop.is_set():
            s = load_settings_row(db_path)
            if s is None or not bool(s["arp_spoof_enabled"]):
                time.sleep(reload_sec)
                continue
            rows = [t for t in load_enabled_targets(db_path) if t["egress_iface"] == iface]
            if not rows:
                time.sleep(reload_sec)
                continue
            try:
                mac = read_iface_mac(iface)
            except OSError as e:
                print(f"arp_sniff[{iface}]: read MAC failed: {e}", flush=True)
                time.sleep(reload_sec)
                continue
            pdst_to_row: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                pdst_to_row[r["spoof_gateway_ip"]] = r
            spoof_set = set(pdst_to_row.keys())

            def handle(pkt: Any) -> None:
                if ARP not in pkt or Ether not in pkt:
                    return
                arp = pkt[ARP]
                if int(arp.op) != 1:
                    return
                pdst = str(arp.pdst)
                psrc = str(arp.psrc)
                if pdst not in spoof_set:
                    return
                row = pdst_to_row.get(pdst)
                if not row:
                    return
                mode = row["policy_mode"]
                if mode == "victim_cidr":
                    nets = parse_victim_nets(row["policy_cidrs"])
                else:
                    nets = []
                if not source_allowed(psrc, nets):
                    return
                eth = pkt[Ether]
                reply = Ether(dst=eth.src, src=mac) / ARP(
                    op=2,
                    hwsrc=mac,
                    psrc=pdst,
                    hwdst=eth.src,
                    pdst=psrc,
                )
                sendp(reply, iface=iface, verbose=0)
                if args.verbose:
                    print(f"arp_sniff[{iface}]: reply who-has {pdst} from {psrc}", flush=True)

            try:
                sniff(iface=iface, filter="arp", prn=handle, store=0, timeout=float(reload_sec))
            except Exception as e:
                print(f"arp_sniff[{iface}]: {e}", flush=True)
                time.sleep(reload_sec)

    print(f"arp_spoof_daemon: db={db_path} garp_interval={garp_interval}s reload={reload_sec}s", flush=True)

    threading.Thread(target=garp_loop, name="arp-garp", daemon=True).start()

    iface_threads: Dict[str, threading.Thread] = {}
    while True:
        try:
            s = load_settings_row(db_path)
            arp_on = s is not None and bool(s["arp_spoof_enabled"])
            targets = load_enabled_targets(db_path) if arp_on else []
            assign_host = not args.no_assign_host_ip
            env_off = os.environ.get("MTR_ARP_ASSIGN_HOST_IP", "1").strip().lower() in (
                "0",
                "false",
                "no",
            )
            desired: set[tuple[str, str]] = set()
            if arp_on and assign_host and not env_off and targets:
                for t in targets:
                    iface = (t.get("egress_iface") or "").strip()
                    ip_s = (t.get("spoof_gateway_ip") or "").strip()
                    if (t.get("satellite_vrf") or "").strip():
                        continue
                    if iface and ip_s:
                        desired.add((iface, ip_s))
            reconcile_assigned_host_ips(db_path, desired, verbose_log=args.verbose)

            if not arp_on or not targets:
                time.sleep(reload_sec)
                continue
            want = set(distinct_ifaces(targets))
            for iface in want:
                if iface not in iface_threads:
                    t = threading.Thread(target=sniff_loop_iface, args=(iface,), name=f"sniff-{iface}", daemon=True)
                    t.start()
                    iface_threads[iface] = t
            time.sleep(reload_sec)
        except KeyboardInterrupt:
            stop.set()
            print("exit", flush=True)
            raise SystemExit(0) from None


if __name__ == "__main__":
    main()
