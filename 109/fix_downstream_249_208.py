#!/usr/bin/env python3
"""109 现网：修复冒充 249 连下游 208（消除 245/247 同对端抢占）。

步骤：
  1. 从 DB / bgp-agent 移除 vbgp245、vbgp247 → 208 下游邻居
  2. 卫星收敛（ipvlan + ip rule + nft DNAT）vbgp13915943249
  3. 重建 vbgp249 → 208（local 249, bind iv249, active）
  4. 补跑 apply_downstream_transit（2110/2111）
  5. 验收 ss / route get / neighbors

用法：python 109/fix_downstream_249_208.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import paramiko

DIR = Path(__file__).resolve().parent
ROOT = DIR.parent
REMOTE = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").rstrip("/")
PEER = os.environ.get("MTR_SATELLITE_PEER_IP", "139.159.43.208").strip()
SPOOF = os.environ.get("RR_ADDR", "139.159.43.249").strip()
VRF = "vbgp13915943249"
CONFLICT_VRFS = ("vbgp13915943245", "vbgp13915943247")
AGENT = "http://127.0.0.1:9179"


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()
        if name == "env":
            break


def _remote_script() -> str:
    conflict = json.dumps(list(CONFLICT_VRFS))
    body_add = json.dumps(
        {
            "vrf": VRF,
            "address": PEER,
            "remote_as": int(os.environ.get("MTR_DOWNSTREAM_REMOTE_AS", "63199")),
            "role": "downstream",
            "local_address": SPOOF,
            "bind_interface": "iv249",
            "passive_mode": False,
        },
        ensure_ascii=False,
    )
    body_rm_249 = json.dumps({"vrf": VRF, "address": PEER}, ensure_ascii=False)
    exports = []
    for k in (
        "MTR_BGP_IPVLAN_AUTO",
        "MTR_BGP_SAT_DNAT_AUTO",
        "MTR_BGP_IPVLAN_BASE_IFACE",
        "MTR_BGP_IPVLAN_PEER_IP",
        "MTR_BGP_RR_UPLINK_IFACE",
        "MTR_BGP_RR_SPOOF_IPVLAN_ADDR",
        "MTR_SATELLITE_PEER_IP",
        "RR_ADDR",
    ):
        v = os.environ.get(k, "").strip()
        if v:
            exports.append(f"export {k}={v!r}")
    if not any("MTR_BGP_IPVLAN_AUTO" in x for x in exports):
        exports.append("export MTR_BGP_IPVLAN_AUTO=1")
    if not any("MTR_BGP_SAT_DNAT_AUTO" in x for x in exports):
        exports.append("export MTR_BGP_SAT_DNAT_AUTO=1")
    if not any("MTR_BGP_RR_SPOOF_IPVLAN_ADDR" in x for x in exports):
        exports.append("export MTR_BGP_RR_SPOOF_IPVLAN_ADDR=1")
    export_block = "\n".join(exports)

    return f"""set -eo pipefail
cd {REMOTE}
[ -f .env ] && set -a && . ./.env && set +a
{export_block}

echo "=== 1) remove conflicting downstream 208 from 245/247 ==="
PY={REMOTE}/venv/bin/python3
[ -x "$PY" ] || PY=python3
export PYTHONPATH={REMOTE}
$PY - <<'PY'
import sqlite3
from pathlib import Path
db = Path({json.dumps(REMOTE + "/data.db")})
conn = sqlite3.connect(str(db))
for vrf in {conflict}:
    cur = conn.execute(
        "DELETE FROM bgp_neighbor_meta WHERE vrf=? AND neighbor_ip=?",
        (vrf, {json.dumps(PEER)}),
    )
    print("db_delete", vrf, cur.rowcount)
conn.commit()
conn.close()
PY

for vrf in vbgp13915943245 vbgp13915943247; do
  curl -sf -X POST {AGENT}/api/neighbors/remove \\
    -H 'Content-Type: application/json' \\
    -d '{{"vrf":"'"$vrf"'","address":{json.dumps(PEER)}}}' \\
    && echo "agent_removed $vrf" || echo "agent_remove_skip $vrf"
done
sleep 2

echo "=== 2) satellite reconcile 249 ==="
grep -q '^MTR_BGP_RR_SPOOF_IPVLAN_ADDR=' {REMOTE}/.env 2>/dev/null && \\
  sed -i 's/^MTR_BGP_RR_SPOOF_IPVLAN_ADDR=.*/MTR_BGP_RR_SPOOF_IPVLAN_ADDR=1/' {REMOTE}/.env || \\
  echo 'MTR_BGP_RR_SPOOF_IPVLAN_ADDR=1' >> {REMOTE}/.env

$PY - <<'PY'
import json
from pathlib import Path
from app import bgp_ipvlan_reconcile
db = Path({json.dumps(REMOTE + "/data.db")})
out = bgp_ipvlan_reconcile.reconcile_vrf_from_op_database(db, {json.dumps(VRF)})
print(json.dumps(out, ensure_ascii=False)[:2000])
if not out.get("ok"):
    import subprocess
    peer, spoof, iv = {json.dumps(PEER)}, {json.dumps(SPOOF)}, "iv249"
    vrf = {json.dumps(VRF)}
    subprocess.run(["ip", "addr", "add", f"{{spoof}}/32", "dev", iv], capture_output=True)
    subprocess.run(["ip", "route", "del", f"{{spoof}}/32", "dev", "enp59s0f0np0"], capture_output=True)
    for cmd in (
        ["ip", "route", "replace", "vrf", vrf, f"{{peer}}/32", "dev", iv, "src", spoof],
        ["ip", "route", "replace", "vrf", vrf, "139.159.43.0/24", "dev", iv, "src", spoof],
    ):
        p = subprocess.run(cmd, capture_output=True, text=True)
        print("fallback", " ".join(cmd), p.returncode, (p.stderr or "").strip()[:120])
PY

echo "=== 3) recycle vbgp249 -> 208 ==="
curl -sf -X POST {AGENT}/api/neighbors/remove \\
  -H 'Content-Type: application/json' -d '{body_rm_249}' || true
sleep 2
curl -sf -X POST {AGENT}/api/neighbors/add \\
  -H 'Content-Type: application/json' -d '{body_add}'
echo
sleep 8

echo "=== 4) verify ==="
echo "nft DNAT 249:"
nft list chain inet mtr_bgp_sat_dnat prerouting 2>/dev/null | grep 249 || echo MISSING
echo "ip rule 249:"
ip -4 rule show | grep 249 || true
echo "route get 208 from 249 (vrf):"
ip vrf exec {VRF} ip route get {PEER} from {SPOOF} 2>&1 | head -1 || true
echo "vrf routes:"
ip route show vrf {VRF} | head -6
echo "ss 208:"
ss -tnp | grep 208 || true
echo "agent neighbors 208:"
curl -sf {AGENT}/api/neighbors | $PY -c "
import json,sys
d=json.load(sys.stdin)
rows=d if isinstance(d,list) else d.get('neighbors') or []
for n in rows:
  a=n.get('address') or n.get('neighbor_ip')
  if a=="{PEER}":
    print(n.get('vrf'), n.get('state') or n.get('session_state'), n.get('local_address') or n.get('source_ip'))
"
curl -sf http://127.0.0.1:8808/api/bgp/neighbors 2>/dev/null | $PY -c "
import json,sys
for n in json.load(sys.stdin):
  if n.get('neighbor_ip')=="{PEER}":
    print('op', n.get('vrf'), n.get('session_state'), n.get('source_ip'))
" 2>/dev/null || true
"""


def main() -> int:
    load_env()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    if not pw:
        print("请配置 109/env 中 MTR_OP_SSH_PASSWORD", file=sys.stderr)
        return 2

    ipvlan = ROOT / "service" / "app" / "bgp_ipvlan_reconcile.py"
    script = _remote_script()

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
    sftp = c.open_sftp()
    if ipvlan.is_file():
        sftp.put(str(ipvlan), f"{REMOTE}/app/bgp_ipvlan_reconcile.py")
    sftp.close()

    i, o, e = c.exec_command("bash -se", timeout=240)
    i.write(script)
    i.channel.shutdown_write()
    out = o.read().decode("utf-8", errors="replace")
    err = e.read().decode("utf-8", errors="replace")
    code = o.channel.recv_exit_status()
    c.close()
    print(out)
    if err.strip():
        print(err, file=sys.stderr)

    if code != 0:
        return code

    print("\n=== apply downstream transit (local) ===")
    sys.path.insert(0, str(DIR))
    from apply_downstream_transit import load_env as load_transit_env, remote_script  # noqa: E402

    load_transit_env()
    c2 = paramiko.SSHClient()
    c2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c2.connect(
        host,
        username=os.environ.get("MTR_OP_SSH_USER", "root"),
        password=pw,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    i2, o2, e2 = c2.exec_command("bash -se", timeout=90)
    i2.write(remote_script("apply"))
    i2.channel.shutdown_write()
    t_out = o2.read().decode("utf-8", errors="replace")
    t_err = e2.read().decode("utf-8", errors="replace")
    t_code = o2.channel.recv_exit_status()
    c2.close()
    print(t_out)
    if t_err.strip():
        print(t_err, file=sys.stderr)
    return t_code if t_code else code


if __name__ == "__main__":
    raise SystemExit(main())
