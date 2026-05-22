#!/usr/bin/env python3
"""在 109（或 env 指定主机）补跑卫星收敛：ipvlan L2 + ip rule + nft DNAT。

用法（默认 SSH 到 109/env）：
  python 109/reconcile_satellite.py
  python 109/reconcile_satellite.py --vrf vbgp13915943249
  python 109/reconcile_satellite.py --spoof 139.159.43.249
  python 109/reconcile_satellite.py --no-recycle-bgp
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import paramiko

DEPLOY_DIR = Path(__file__).resolve().parent
ROOT = DEPLOY_DIR.parent
REMOTE_DIR = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").rstrip("/")
REMOTE_DB = f"{REMOTE_DIR}/data.db"
OP_PORT = os.environ.get("MTR_OP_PORT", "8808").strip()

# 收敛依赖的环境变量（与 109/env.example 一致）
_EXPORT_KEYS = (
    "MTR_BGP_IPVLAN_AUTO",
    "MTR_BGP_SAT_DNAT_AUTO",
    "MTR_BGP_IPVLAN_BASE_IFACE",
    "MTR_BGP_IPVLAN_PEER_IP",
    "MTR_BGP_RR_UPLINK_IFACE",
    "MTR_BGP_RR_SPOOF_IPVLAN_ADDR",
    "MTR_SATELLITE_PEER_IP",
    "MTR_SATELLITE_BGP_TCP_SOURCE",
    "RR_ADDR",
    "ROUTER_ID",
    "LOCAL_AS",
)


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DEPLOY_DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:
                os.environ[k] = v
        if name == "env":
            break


def _shell_export() -> str:
    lines = []
    for k in _EXPORT_KEYS:
        v = os.environ.get(k, "").strip()
        if v:
            lines.append(f"export {k}={v!r}")
    if not os.environ.get("MTR_BGP_IPVLAN_AUTO", "").strip():
        lines.append("export MTR_BGP_IPVLAN_AUTO=1")
    if not os.environ.get("MTR_BGP_SAT_DNAT_AUTO", "").strip():
        lines.append("export MTR_BGP_SAT_DNAT_AUTO=1")
    return "\n".join(lines)


def _remote_python_reconcile(vrf: str, spoof: str) -> str:
    vrf_lit = json.dumps(vrf)
    spoof_lit = json.dumps(spoof)
    return f"""
set -e
cd {REMOTE_DIR}
[ -f .env ] && set -a && . ./.env && set +a
{_shell_export()}
PY={REMOTE_DIR}/venv/bin/python3
if [ ! -x "$PY" ]; then PY=python3; fi
export PYTHONPATH={REMOTE_DIR}
$PY - <<'PY'
import json
import os
from pathlib import Path

from app import bgp_ipvlan_reconcile

db = Path({json.dumps(REMOTE_DB)})
vrf = {vrf_lit}
spoof = {spoof_lit}

stack = bgp_ipvlan_reconcile.ensure_lab_network_stack(db)
if vrf:
    out = bgp_ipvlan_reconcile.reconcile_vrf_from_op_database(db, vrf)
else:
    out = bgp_ipvlan_reconcile.reconcile_from_op_database(db)
print(json.dumps({{"stack": stack, "reconcile": out}}, ensure_ascii=False, indent=2))
PY
"""


def _tx_port_for_vrf(vrf: str) -> int:
    h = 0
    for ch in vrf:
        h = (h * 31 + ord(ch)) & 0xFFFF
    return 1790 + 1 + (h % 50)


def _remote_verify(spoof: str, peer: str, vrf: str) -> str:
    port = _tx_port_for_vrf(vrf) if vrf else 1830
    return f"""
echo "=== verify ==="
echo "nft 249:"
nft list table inet mtr_bgp_sat_dnat 2>/dev/null | grep -E '{spoof}|redirect' || echo MISSING_DNAT
echo "ip rule 249:"
ip -4 rule show | grep '{spoof}' || echo NO_RULES
echo "route get peer from spoof:"
ip route get {peer} from {spoof} 2>&1 | head -1
ip vrf exec {vrf} ip route get {peer} from {spoof} 2>&1 | head -1
echo "iv249 addr:"
ip -4 addr show dev iv249 2>/dev/null | grep inet || true
echo "vrf routes:"
ip route show vrf {vrf} 2>/dev/null | grep -E '{peer}|249' | head -5
echo "TX listen (expect port {port}):"
ss -tlnp | grep -E ':{port}\\b' || true
"""


def _remote_recycle_bgp(vrf: str, peer: str, spoof: str) -> str:
    bind = "iv" + spoof.rsplit(".", 1)[-1]
    body_add = json.dumps(
        {
            "vrf": vrf,
            "address": peer,
            "remote_as": int(os.environ.get("MTR_DOWNSTREAM_REMOTE_AS", "63199")),
            "role": "downstream",
            "local_address": spoof,
            "bind_interface": bind,
            "passive_mode": False,
        },
        ensure_ascii=False,
    )
    body_rm = json.dumps({"vrf": vrf, "address": peer}, ensure_ascii=False)
    return f"""
echo "=== recycle bgp-agent neighbor ==="
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/remove \\
  -H 'Content-Type: application/json' -d '{body_rm}' || true
sleep 2
curl -sf -X POST http://127.0.0.1:9179/api/neighbors/add \\
  -H 'Content-Type: application/json' -d '{body_add}'
echo
sleep 6
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors') or []:
  if n.get('address')=='{peer}':
    print(json.dumps(n,ensure_ascii=False,indent=2))
"
curl -sf 'http://127.0.0.1:8808/api/bgp/neighbors?vrf={vrf}' | python3 -c "
import json,sys
d=json.load(sys.stdin)
rows=d if isinstance(d,list) else d.get('neighbors') or []
for n in rows:
  if n.get('neighbor_ip')=='{peer}' or n.get('address')=='{peer}':
    print('op', n.get('session_state'))
" 2>/dev/null || true
"""


def run_remote(
    *,
    vrf: str,
    spoof: str,
    peer: str,
    recycle_bgp: bool,
) -> int:
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    if not pw:
        print("请配置 109/env 中的 MTR_OP_SSH_PASSWORD", file=sys.stderr)
        return 2

    script = _remote_python_reconcile(vrf, spoof)
    script += _remote_verify(spoof, peer, vrf or "vbgp13915943249")
    if recycle_bgp and vrf and peer:
        script += _remote_recycle_bgp(vrf, peer, spoof)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        host,
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=pw,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    i, o, e = c.exec_command("bash -se", timeout=180)
    i.write(script)
    i.channel.shutdown_write()
    out = o.read().decode("utf-8", errors="replace")
    err = e.read().decode("utf-8", errors="replace")
    code = o.channel.recv_exit_status()
    c.close()
    print(out)
    if err.strip():
        print(err, file=sys.stderr)
    return code


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description="补跑 109 卫星 ipvlan + DNAT + ip rule 收敛")
    ap.add_argument("--vrf", default="vbgp13915943249", help="仅收敛指定卫星 VRF")
    ap.add_argument("--spoof", default="139.159.43.249", help="冒充网关 IP（校验用）")
    ap.add_argument(
        "--peer",
        default=os.environ.get("MTR_BGP_IPVLAN_PEER_IP", "139.159.43.208"),
        help="下游 BGP 对端",
    )
    ap.add_argument(
        "--no-recycle-bgp",
        action="store_true",
        help="收敛后不重建 bgp-agent 下游邻居",
    )
    args = ap.parse_args()
    code = run_remote(
        vrf=(args.vrf or "").strip(),
        spoof=args.spoof.strip(),
        peer=args.peer.strip(),
        recycle_bgp=not args.no_recycle_bgp,
    )
    if code == 0:
        print("reconcile_satellite_ok")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
