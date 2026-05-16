"""
撤掉 ARP 冒充网关 /32 后，尽量刷新二层邻居对「该 IPv4 的 MAC」的认知。

问题：引流期间本机周期发 GARP，下游（如 Linux 201）会把网关 IP 绑到本机接口 MAC；仅删库删 /32
不会通知下游，故 ``ip neigh show <网关>`` 仍可能长期 STALE 指向错误 lladdr。

做法（Linux、尽力而为）：
1. 在 ``ip addr del`` 之后 ``ip route get <spoof_ip> oif <iface>`` 解析 ``via``；
2. ``ip neigh get <via> dev <iface>`` 取真实下一跳 lladdr；
3. 若已安装 **scapy**，向该接口发若干次 gratuitous ARP（op=2），以太网源与 ARP hwsrc 均为该 lladdr，
   psrc=pdst=spoof_ip，通知网段「该 IP 的 MAC 恢复为下一跳设备」。

可通过环境变量 ``MTR_OP_ARP_RESTORE_NEIGH`` / ``MTR_ARP_RESTORE_NEIGH``（默认 ``1``）关闭。
"""
from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


def _restore_enabled() -> bool:
    for key in ("MTR_OP_ARP_RESTORE_NEIGH", "MTR_ARP_RESTORE_NEIGH"):
        raw = (os.environ.get(key) or "").strip().lower()
        if raw in {"0", "false", "no"}:
            return False
    return True


def _ip(args: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["ip", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def _parse_via(route_out: str) -> Optional[str]:
    # "10.133.152.250 via 10.133.152.1 dev ens192 src ..." or multiline
    line = route_out.replace("\n", " ").strip()
    parts = line.split()
    for i, w in enumerate(parts):
        if w == "via" and i + 1 < len(parts):
            cand = parts[i + 1]
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", cand):
                return cand
    return None


def _parse_lladdr(neigh_out: str) -> Optional[str]:
    m = re.search(r"lladdr\s+([0-9a-f:]{17}|[0-9a-f:]{11,})", neigh_out, re.I)
    if not m:
        return None
    return m.group(1).strip().lower()


def resolve_nexthop_lladdr(spoof_ip: str, iface: str) -> Optional[str]:
    """在已撤掉本机 ``spoof_ip/32`` 的前提下，解析经 ``iface`` 去往 ``spoof_ip`` 的下一跳 MAC。"""
    code, out, _ = _ip(["route", "get", spoof_ip, "oif", iface])
    if code != 0 or not out:
        code, out, _ = _ip(["route", "get", spoof_ip])
    if code != 0 or not out:
        return None
    via = _parse_via(out)
    if not via:
        return None
    code2, out2, _ = _ip(["neigh", "get", via, "dev", iface])
    if code2 != 0:
        return None
    return _parse_lladdr(out2)


def _local_neigh_del(spoof_ip: str, iface: str) -> None:
    code, _, err = _ip(["neigh", "del", spoof_ip, "dev", iface])
    if code != 0 and "error" in err.lower():
        logger.debug("arp_restore: local neigh del %s dev %s: %s", spoof_ip, iface, err)


def send_gratuitous_arp_restore(iface: str, spoof_ip: str, restore_hwsrc: str, bursts: int = 3) -> bool:
    """发 gratuitous ARP。优先当前解释器内的 scapy；否则尝试 ``/usr/bin/python3``（常见于 apt 的 python3-scapy）。"""
    hw = restore_hwsrc.strip().lower()
    bursts = max(1, bursts)

    def _send_with_scapy() -> bool:
        from scapy.all import ARP, Ether, sendp  # type: ignore[import-not-found]

        pkt = Ether(dst="ff:ff:ff:ff:ff:ff", src=hw) / ARP(
            op=2,
            hwsrc=hw,
            psrc=spoof_ip,
            pdst=spoof_ip,
            hwdst="00:00:00:00:00:00",
        )
        for _ in range(bursts):
            sendp(pkt, iface=iface, verbose=0)
        return True

    try:
        _send_with_scapy()
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.warning("arp_restore: scapy sendp failed iface=%s: %s", iface, e)
        return False

    alt = "/usr/bin/python3"
    if not os.path.isfile(alt):
        logger.warning(
            "arp_restore: 当前 Python 无 scapy 且未找到 %s；无法发恢复 GARP；对端可: ip neigh del %s dev <iface>",
            alt,
            spoof_ip,
        )
        return False
    code = subprocess.run(
        [
            alt,
            "-c",
            "import sys;"
            "from scapy.all import ARP,Ether,sendp;"
            "iface,ip,hw,n=sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4]);"
            "pkt=Ether(dst='ff:ff:ff:ff:ff:ff',src=hw)/ARP(op=2,hwsrc=hw,psrc=ip,pdst=ip,hwdst='00:00:00:00:00:00');"
            "[sendp(pkt,iface=iface,verbose=0) for _ in range(max(1,n))]",
            iface,
            spoof_ip,
            hw,
            str(bursts),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if code.returncode != 0:
        err = (code.stderr or code.stdout or "").strip()
        logger.warning("arp_restore: %s -c scapy failed: %s", alt, err[:500])
        return False
    return True


def restore_after_spoof_removed(iface: str, spoof_ip: str, *, bursts: int = 3) -> None:
    """
    在 ``remove_ipv4_secondary`` 之后调用：解析下一跳 MAC 并广播恢复 GARP；顺带清理本机邻居项。
    非 Linux 或未开启时 no-op。
    """
    if platform.system() != "Linux":
        return
    if not _restore_enabled():
        return
    if not iface.strip() or not spoof_ip.strip():
        return
    ll = resolve_nexthop_lladdr(spoof_ip.strip(), iface.strip())
    if not ll:
        logger.info(
            "arp_restore: 无法解析 %s 经 %s 的下一跳 lladdr（略过 GARP）；"
            "对端若仍见错误 neigh 可: ip neigh del %s dev <对端出接口>",
            spoof_ip,
            iface,
            spoof_ip,
        )
        _local_neigh_del(spoof_ip.strip(), iface.strip())
        return
    if send_gratuitous_arp_restore(iface.strip(), spoof_ip.strip(), ll, bursts=bursts):
        logger.info(
            "arp_restore: 已发恢复 GARP spoof=%s iface=%s restore_hwsrc=%s",
            spoof_ip,
            iface,
            ll,
        )
    _local_neigh_del(spoof_ip.strip(), iface.strip())
