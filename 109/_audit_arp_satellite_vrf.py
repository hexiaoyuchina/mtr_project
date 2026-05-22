#!/usr/bin/env python3
"""Audit ARP 引流条目 vs 内核卫星 VRF（对照 Web 界面数据）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import paramiko

DEPLOY_DIR = Path(__file__).resolve().parent


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY_DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()
        if name == "env":
            break


REMOTE = r"""
import json, sqlite3, subprocess, re
from pathlib import Path

def vrf_expected(spoof):
    return "vbgp" + spoof.replace(".", "")

db = Path("/root/mtr_op/data.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

print("=" * 60)
print("ARP 引流总开关")
print("=" * 60)
for r in conn.execute("SELECT * FROM arp_spoof_settings"):
    print(dict(r))

print()
print("=" * 60)
print("界面等价: GET /api/arp-spoof/targets (全部条目)")
print("=" * 60)
cols = [d[1] for d in conn.execute("PRAGMA table_info(arp_spoof_targets)")]
print("列:", ", ".join(cols))
rows = list(conn.execute(
    "SELECT * FROM arp_spoof_targets ORDER BY id"
))
print("条数:", len(rows))
print()

# kernel state
kernel_vrfs = set()
try:
    p = subprocess.run(["ip", "-br", "link", "show", "type", "vrf"], capture_output=True, text=True)
    for line in (p.stdout or "").splitlines():
        name = line.split()[0] if line.strip() else ""
        if name.startswith("vbgp"):
            kernel_vrfs.add(name)
except Exception:
    pass

ipvlan_by_spoof = {}
st = Path("/root/mtr_op/.bgp_ipvlan_reconcile.json")
if st.is_file():
    try:
        ipvlan_by_spoof = json.load(open(st)).get("by_spoof_ip") or {}
    except Exception:
        pass

legacy_sat = {}
st2 = Path("/root/mtr_op/.satellite_vrf_assign.json")
if st2.is_file():
    try:
        legacy_sat = json.load(open(st2)).get("by_spoof_ip") or {}
    except Exception:
        pass

issues = []
enabled_n = 0
for r in rows:
    d = dict(r)
    spoof = (d.get("spoof_gateway_ip") or "").strip()
    vrf_db = (d.get("satellite_vrf") or "").strip()
    iface = (d.get("egress_iface") or "").strip()
    en = bool(d.get("enabled"))
    exp_vrf = vrf_expected(spoof) if spoof else ""
    iv = f"iv{spoof.split('.')[-1]}" if spoof and spoof.count('.') == 3 else ""

    # kernel checks
    vrf_ok = vrf_db in kernel_vrfs if vrf_db else False
    iv_exists = False
    has_32 = False
    route_208 = False
    peer = "139.159.43.208"
    if vrf_db and iv:
        p1 = subprocess.run(["ip", "link", "show", iv], capture_output=True)
        iv_exists = p1.returncode == 0
        p2 = subprocess.run(["ip", "addr", "show", iv], capture_output=True, text=True)
        has_32 = spoof in (p2.stdout or "")
        p3 = subprocess.run(["ip", "route", "show", "vrf", vrf_db], capture_output=True, text=True)
        route_208 = peer in (p3.stdout or "")

    ui_vrf_status = "已创建" if vrf_db else "未创建"

    print(f"--- ID={d.get('id')} enabled={en} UI_VRF状态={ui_vrf_status} ---")
    print(f"  冒充IP: {spoof}")
    print(f"  satellite_vrf(DB): {vrf_db or '(空)'}")
    print(f"  命名应为: {exp_vrf}" + (" OK" if vrf_db == exp_vrf else " MISMATCH" if vrf_db else ""))
    print(f"  出接口: {iface}")
    print(f"  策略: {d.get('policy_mode')}  cidrs: {d.get('policy_cidrs') or '-'}")
    print(f"  备注: {(d.get('note') or '')[:60]}")
    print(f"  内核VRF存在: {vrf_ok}  ivlan={iv_exists}  {spoof}/32 on iv: {has_32}  路由含208: {route_208}")
    if spoof in ipvlan_by_spoof:
        print(f"  ipvlan状态: {ipvlan_by_spoof[spoof]}")
    if spoof in legacy_sat:
        print(f"  legacy卫星分配: {legacy_sat[spoof]}")

    if en:
        enabled_n += 1
    if en and not vrf_db:
        issues.append(f"ID{d.get('id')} {spoof}: 启用但 satellite_vrf 为空(界面显示未创建)")
    if vrf_db and vrf_db != exp_vrf:
        issues.append(f"ID{d.get('id')} {spoof}: VRF名 {vrf_db} != 规范 {exp_vrf}")
    if en and vrf_db and iface != "eno1np0":
        issues.append(f"ID{d.get('id')} {spoof}: 出接口 {iface} 非现网下联 eno1np0")
    if en and vrf_db and not vrf_ok:
        issues.append(f"ID{d.get('id')} {spoof}: DB有VRF {vrf_db} 但内核无此VRF")
    if en and vrf_db and vrf_ok and not has_32:
        issues.append(f"ID{d.get('id')} {spoof}: VRF在但 iv 无 {spoof}/32 (ping/BGP源可能不通)")
    if en and vrf_db and vrf_ok and not route_208:
        issues.append(f"ID{d.get('id')} {spoof}: VRF无到 208 路由 (208 ping 不通的主因)")

print()
print("=" * 60)
print(f"启用条目: {enabled_n} / {len(rows)}")
print("内核 vbgp* VRF 数量:", len(kernel_vrfs))
print("ipvlan 状态跟踪 IP 数:", len(ipvlan_by_spoof))
print("legacy 卫星分配 IP 数:", len(legacy_sat))

# orphan kernel vrfs
db_vrfs = {dict(r).get("satellite_vrf", "").strip() for r in rows if dict(r).get("satellite_vrf")}
orphan_k = sorted(kernel_vrfs - {v for v in db_vrfs if v})
orphan_db = sorted({v for v in db_vrfs if v and v not in kernel_vrfs})
if orphan_k:
    print()
    print("内核有、ARP表未登记的 vbgp* (前20):", orphan_k[:20], ("..." if len(orphan_k) > 20 else ""))
if orphan_db:
    print("ARP表有、内核无的 VRF:", orphan_db)

print()
print("=" * 60)
print("重点: 245/247/249")
print("=" * 60)
for spoof in ("139.159.43.245", "139.159.43.247", "139.159.43.249"):
    match = [dict(r) for r in rows if dict(r).get("spoof_gateway_ip") == spoof]
    print(spoof, "条目数", len(match))
    for m in match:
        print(" ", m)

print()
print("=" * 60)
print("问题汇总 (" + str(len(issues)) + ")")
print("=" * 60)
for i in issues[:80]:
    print(" -", i)
if len(issues) > 80:
    print(f" ... 另有 {len(issues)-80} 条")

conn.close()
"""


def main() -> None:
    load_env()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        os.environ["MTR_OP_HOST"],
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=os.environ["MTR_OP_SSH_PASSWORD"],
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    _, stdout, stderr = c.exec_command(f"python3 <<'PY'\n{REMOTE}\nPY", timeout=180)
    print(stdout.read().decode("utf-8", errors="replace"))
    err = stderr.read().decode("utf-8", errors="replace")
    if err.strip():
        print("STDERR:", err)
    # also curl API
    _, o2, _ = c.exec_command(
        "curl -sf http://127.0.0.1:8808/api/arp-spoof/targets | python3 -c \"import sys,json; t=json.load(sys.stdin); print('API条数',len(t)); e=sum(1 for x in t if x.get('enabled')); print('API启用',e)\"",
        timeout=30,
    )
    print(o2.read().decode())
    c.close()


if __name__ == "__main__":
    main()
