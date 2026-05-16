"""Remove cross-vrf transit routes added by enable_cross_vrf_transit.py (physical ports).

RouterOS 上仅删除 comment=cross-vrf-transit 的静态，不会动 BGP peer；与 apply_bgp_linux200_ros.py 并存时也可执行清理。
"""
import os
import sys

import paramiko

PW = os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")
H200 = (os.environ.get("MTR_OP_HOST") or "").strip()
H_CLIENT_A = (os.environ.get("MTR_OP_VERIFIER_201") or "").strip()
H_CLIENT_B = (os.environ.get("MTR_OP_VERIFIER_202") or "").strip()
H_ROS = (os.environ.get("MTR_LAB_ROUTEROS") or "").strip()


def run(host: str, user: str, label: str, cmds: list[str]) -> None:
    print(f"\n{'='*60}\n{label}\n{'='*60}")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=user, password=PW, timeout=30, allow_agent=False, look_for_keys=False)
    for cmd in cmds:
        print(f"$ {cmd}")
        _, o, e = c.exec_command(cmd)
        print((o.read() + e.read()).decode("utf-8", errors="replace"), end="")
    c.close()


def main() -> None:
    if not H200 or not H_CLIENT_A or not H_CLIENT_B or not H_ROS:
        print(
            "需要 MTR_OP_HOST、MTR_OP_VERIFIER_201、MTR_OP_VERIFIER_202、MTR_LAB_ROUTEROS",
            file=sys.stderr,
        )
        raise SystemExit(2)
    run(
        H200,
        "root",
        "OP 主机 — 删除跨 VRF 路由与 veth（与 enable_cross_vrf_transit 对应）",
        [
            "ip route del 10.133.153.204/32 vrf vrf2102 2>/dev/null || true",
            "ip route del 10.133.153.205/32 vrf vrf2102 2>/dev/null || true",
            "ip route del 10.133.152.204/32 vrf vrf2103 2>/dev/null || true",
            "ip route del 10.133.152.205/32 vrf vrf2103 2>/dev/null || true",
            "ip link del vrftrans2102 2>/dev/null || true",
            # sysctl -w net.ipv4.ip_forward=0  # 若其它用途仍需转发请不要执行
        ],
    )
    run(
        H_CLIENT_A,
        "root",
        "客户端 A — 删除 →153.204 / →153.205 主机路由",
        [
            "ip route del 10.133.153.204/32 vrf vrf2102 2>/dev/null || true",
            "ip route del 10.133.153.205/32 vrf vrf2102 2>/dev/null || true",
        ],
    )
    run(
        H_CLIENT_B,
        "root",
        "客户端 B — 删除 →153.204 / →153.205 主机路由",
        [
            "ip route del 10.133.153.204/32 vrf vrf2102 2>/dev/null || true",
            "ip route del 10.133.153.205/32 vrf vrf2102 2>/dev/null || true",
        ],
    )
    run(
        H_ROS,
        "admin",
        "RouterOS — 删除 cross-vrf-transit 静态路由",
        [r'/ip route remove [find comment="cross-vrf-transit"]'],
    )


if __name__ == "__main__":
    main()
