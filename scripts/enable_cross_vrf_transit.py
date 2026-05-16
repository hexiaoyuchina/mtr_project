"""Enable Linux 200 as transit: vrf2102 (152) <-> vrf2103 (153).

Maps per step.md **physical** interfaces:
  OP host: ens192/.200, ens161/.201, ens224/.200, ens256/.201
  Client A: ens192 -> 153.204 & 153.205 via 152.200
  Client B: ens192 -> 153.204 & 153.205 via 152.201
  ROS（默认）: 静态 cross-vrf-transit：152.204/32→153.200，152.205/32→153.201

Requires: base VRF + addressing already applied (第三节 3.2、第四至六节推荐）。

Linux 200：优先尝试 **nexthop-vrf**（较新 iproute2）；若不支持则自动创建 **veth**
`vrftrans2102` <-> `vrftrans2103`（10.255.210.0/30）互联两 VRF（兼容旧 iproute2-ss200127）。

环境变量 **MTR_CROSS_VRF_ROS_VIA_BGP=1**（与 scripts/apply_bgp_linux200_ros.py 配套）时：
  - 仍配置 Linux 200 跨 VRF / vrf2102 default；**不写** vrf2103 经 ens224 的**内核静态 default**（避免与 BGP 学到的默认路由冲突）。
  - 在 RouterOS 上**仅删除** comment=cross-vrf-transit 的旧静态（清理历史配置），**不再添加**两条 /32 静态；152 回程改由 eBGP 宣告。
"""
from __future__ import annotations

import os
import sys

import paramiko

PW = os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")
H200 = (os.environ.get("MTR_OP_HOST") or "").strip()
H_CLIENT_A = (os.environ.get("MTR_OP_VERIFIER_201") or "").strip()
H_CLIENT_B = (os.environ.get("MTR_OP_VERIFIER_202") or "").strip()
H_ROS = (os.environ.get("MTR_LAB_ROUTEROS") or "").strip()
# 与 scripts/apply_flat152_no_vrf.py 配合：客户端已在主表配置经 200 的 153 路由，勿再写 vrf 内静态路由
SKIP_CLIENT_ROUTES = os.environ.get("MTR_CROSS_VRF_SKIP_CLIENTS") == "1"
# 与 scripts/apply_bgp_linux200_ros.py 配套：ROS↔200 回程走 BGP，勿再写 cross-vrf-transit 静态，勿写 vrf2103 静态 default
ROS_VIA_BGP = os.environ.get("MTR_CROSS_VRF_ROS_VIA_BGP") == "1"

# 插入 LINUX200_SCRIPT：静态 default vs BGP 模式
_VRF2103_DEFAULT_STATIC = r"""ip route replace vrf vrf2103 default via 10.133.153.204 dev ens224 2>/dev/null && echo "### vrf2103 default via ens224 -> ROS"
"""
_VRF2103_DEFAULT_BGP = r"""# MTR_CROSS_VRF_ROS_VIA_BGP=1：默认路由由 zebra/BGP 安装，勿写内核静态 default
echo "### vrf2103 static default 已跳过（ROS 回程 = BGP）"
"""

# Must match disable_cross_vrf_transit.py
VETH_A = "vrftrans2102"
VETH_B = "vrftrans2103"


def run_ssh(host: str, user: str, cmds: list[str], label: str) -> None:
    print(f"\n{'='*60}\n{label}\n{'='*60}")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=user, password=PW, timeout=30, allow_agent=False, look_for_keys=False)
    for cmd in cmds:
        print(f"$ {cmd}")
        _, o, e = c.exec_command(cmd)
        out = o.read().decode("utf-8", errors="replace") + e.read().decode("utf-8", errors="replace")
        print(out, end="" if out.endswith("\n") else out + "\n")
    c.close()


def run_bash_script(host: str, user: str, script: str, label: str) -> None:
    print(f"\n{'='*60}\n{label}\n{'='*60}")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=user, password=PW, timeout=30, allow_agent=False, look_for_keys=False)
    stdin, stdout, stderr = c.exec_command("bash -s")
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace") + stderr.read().decode("utf-8", errors="replace")
    print(out, end="" if out.endswith("\n") else out + "\n")
    c.close()


LINUX200_SCRIPT = rf"""
set +e
sysctl -w net.ipv4.ip_forward=1
for i in all default ens192 ens161 ens224 ens256 {VETH_A} {VETH_B}; do
  sysctl -w net.ipv4.conf.${{i}}.rp_filter=2 2>/dev/null || true
done

if ip route replace vrf vrf2102 10.133.153.204/32 nexthop-vrf vrf2103 via 10.133.153.204 dev ens224 2>/dev/null; then
  ip route replace vrf vrf2102 10.133.153.205/32 nexthop-vrf vrf2103 via 10.133.153.205 dev ens256
  ip route replace vrf vrf2103 10.133.152.204/32 nexthop-vrf vrf2102 via 10.133.152.204 dev ens192
  ip route replace vrf vrf2103 10.133.152.205/32 nexthop-vrf vrf2102 via 10.133.152.205 dev ens161
  echo "### Linux 200: inter-VRF routes via nexthop-vrf"
else
  ip link show {VETH_A} >/dev/null 2>&1 || ip link add {VETH_A} type veth peer name {VETH_B}
  ip link set {VETH_A} master vrf2102
  ip link set {VETH_B} master vrf2103
  ip addr flush dev {VETH_A} 2>/dev/null || true
  ip addr flush dev {VETH_B} 2>/dev/null || true
  ip addr add 10.255.210.1/30 dev {VETH_A}
  ip addr add 10.255.210.2/30 dev {VETH_B}
  ip link set {VETH_A} up
  ip link set {VETH_B} up
  for i in {VETH_A} {VETH_B}; do sysctl -w net.ipv4.conf.${{i}}.rp_filter=2 2>/dev/null || true; done
  ip route replace vrf vrf2102 10.133.153.204/32 via 10.255.210.2 dev {VETH_A}
  ip route replace vrf vrf2102 10.133.153.205/32 via 10.255.210.2 dev {VETH_A}
  ip route replace vrf vrf2103 10.133.152.204/32 via 10.255.210.1 dev {VETH_B}
  ip route replace vrf vrf2103 10.133.152.205/32 via 10.255.210.1 dev {VETH_B}
  echo "### Linux 200: inter-VRF via veth {VETH_A} <-> {VETH_B} (10.255.210.0/30)"
fi

# 152 侧访问公网：vrf2102 必须有 default 进入 vrf2103，再走 ROS（仅跨段 /32 不够）
# 优先 veth 路径；否则 nexthop-vrf（与 enable_lab_internet_routes.py 一致）
if ip link show {VETH_A} >/dev/null 2>&1 && ip route replace vrf vrf2102 default via 10.255.210.2 dev {VETH_A} 2>/dev/null; then
  echo "### vrf2102 default via {VETH_A} peer -> vrf2103"
elif ip route replace vrf vrf2102 default nexthop-vrf vrf2103 via 10.133.153.204 dev ens224 2>/dev/null; then
  echo "### vrf2102 default nexthop-vrf -> vrf2103 (ens224)"
else
  echo "### WARN: vrf2102 default route not set (check iproute2 / VRF)"
fi
<<<VRF2103_DEFAULT_BLOCK>>>
echo "--- vrf2102 (跨段或 veth)"
ip route show vrf vrf2102 | grep -E '153\\.(204|205)|255\\.210|vrftrans' || true
echo "--- vrf2103 (跨段或 veth)"
ip route show vrf vrf2103 | grep -E '152\\.(204|205)|255\\.210|vrftrans' || true
"""

LINUX200_SCRIPT = LINUX200_SCRIPT.replace(
    "<<<VRF2103_DEFAULT_BLOCK>>>",
    _VRF2103_DEFAULT_BGP if ROS_VIA_BGP else _VRF2103_DEFAULT_STATIC,
)


def lab_client_a_cmds() -> list[str]:
    return [
        "ip route replace 10.133.153.204/32 via 10.133.152.200 dev ens192 vrf vrf2102",
        "ip route replace 10.133.153.205/32 via 10.133.152.200 dev ens192 vrf vrf2102",
        r"ip route show vrf vrf2102 | grep 153 || true",
    ]


def lab_client_b_cmds() -> list[str]:
    return [
        "ip route replace 10.133.153.204/32 via 10.133.152.201 dev ens192 vrf vrf2102",
        "ip route replace 10.133.153.205/32 via 10.133.152.201 dev ens192 vrf vrf2102",
        r"ip route show vrf vrf2102 | grep 153 || true",
    ]


def routeros_cmds() -> list[str]:
    """静态回程：删旧再加两条 cross-vrf-transit。"""
    return [
        r'/ip route remove [find comment="cross-vrf-transit"]',
        r'/ip route add dst-address=10.133.152.204/32 gateway=10.133.153.200 comment="cross-vrf-transit"',
        r'/ip route add dst-address=10.133.152.205/32 gateway=10.133.153.201 comment="cross-vrf-transit"',
        r'/ip route print where comment="cross-vrf-transit"',
    ]


def routeros_cmds_bgp_mode() -> list[str]:
    """仅清理历史静态，不添加；与 apply_bgp_linux200_ros.py 的 eBGP 回程兼容。"""
    return [
        r'/ip route remove [find comment="cross-vrf-transit"]',
        r'/ip route print where comment="cross-vrf-transit"',
    ]


def verify_cmds() -> None:
    if SKIP_CLIENT_ROUTES:
        a_cmds = [
            "ping -c 3 -W 2 10.133.153.204",
            "ping -c 3 -W 2 10.133.153.205",
        ]
        b_cmds = list(a_cmds)
        la = "验证 — 客户端 A（主表，无 vrf）→ 153.204 / 153.205"
        lb = "验证 — 客户端 B（主表，无 vrf）→ 153.204 / 153.205"
    else:
        a_cmds = [
            "ip vrf exec vrf2102 ping -c 3 -W 2 10.133.153.204",
            "ip vrf exec vrf2102 ping -c 3 -W 2 10.133.153.205",
        ]
        b_cmds = list(a_cmds)
        la = "验证 — 客户端 A vrf2102 → 153.204 / 153.205"
        lb = "验证 — 客户端 B vrf2102 → 153.204 / 153.205"
    if H_CLIENT_A:
        run_ssh(H_CLIENT_A, "root", a_cmds, la)
    if H_CLIENT_B:
        run_ssh(H_CLIENT_B, "root", b_cmds, lb)
    if H_ROS:
        run_ssh(
            H_ROS,
            "admin",
            [
                r'/ping count=3 10.133.152.204',
                r'/ping count=3 10.133.152.205',
            ],
            "验证 — RouterOS → Linux 152.204 / 152.205",
        )


def main() -> None:
    if not H200:
        print("需要 MTR_OP_HOST（Linux 200 管理地址）", file=sys.stderr)
        raise SystemExit(2)
    if ROS_VIA_BGP:
        print(
            ">>> MTR_CROSS_VRF_ROS_VIA_BGP=1：ROS 不写 cross-vrf-transit 静态；"
            "Linux vrf2103 不写内核静态 default（回程依赖 apply_bgp_linux200_ros.py 的 eBGP）。",
            flush=True,
        )
    if not SKIP_CLIENT_ROUTES and (not H_CLIENT_A or not H_CLIENT_B):
        print(
            "需要 MTR_OP_VERIFIER_201、MTR_OP_VERIFIER_202；"
            "若已用 apply_flat152_no_vrf.py 仅配客户端，可设 MTR_CROSS_VRF_SKIP_CLIENTS=1",
            file=sys.stderr,
        )
        raise SystemExit(2)
    run_bash_script(H200, "root", LINUX200_SCRIPT, "OP 主机 — 转发 + 跨 VRF（nexthop-vrf 或 veth）")
    if not SKIP_CLIENT_ROUTES:
        run_ssh(H_CLIENT_A, "root", lab_client_a_cmds(), "客户端 A — 静态路由 → 153.204、153.205")
        run_ssh(H_CLIENT_B, "root", lab_client_b_cmds(), "客户端 B — 静态路由 → 153.204、153.205")
    if H_ROS:
        if ROS_VIA_BGP:
            run_ssh(
                H_ROS,
                "admin",
                routeros_cmds_bgp_mode(),
                "RouterOS — 仅删除 cross-vrf-transit（MTR_CROSS_VRF_ROS_VIA_BGP=1，不加静态）",
            )
        else:
            run_ssh(H_ROS, "admin", routeros_cmds(), "RouterOS — 回程静态路由（cross-vrf-transit）")
    elif SKIP_CLIENT_ROUTES:
        print("\n（跳过 RouterOS：未设置 MTR_LAB_ROUTEROS，回程可能不通）")
    verify_cmds()
    done = (
        "\n完成。Linux 200 若不支持 nexthop-vrf，已自动使用 veth vrftrans2102/vrftrans2103；详见 step.md 第十二节。"
    )
    if ROS_VIA_BGP:
        done += (
            "\n提示：已设置 MTR_CROSS_VRF_ROS_VIA_BGP=1，未添加 ROS cross-vrf-transit 静态；"
            "请确保已运行 scripts/apply_bgp_linux200_ros.py（或等价 FRR/ROS eBGP），否则 152↔153 回程可能不通。"
        )
    print(done)


if __name__ == "__main__":
    main()
