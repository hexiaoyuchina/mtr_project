#!/usr/bin/env python3
import os, sys
from pathlib import Path
import paramiko

DEPLOY_DIR = Path(__file__).resolve().parent

def load_env():
    for name in ("env", "env.example"):
        p = DEPLOY_DIR / name
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            if name == "env":
                break

def run(c, script):
    i, o, e = c.exec_command("bash -se", timeout=90)
    i.write(script)
    i.channel.shutdown_write()
    return o.read().decode(errors="replace") + e.read().decode(errors="replace")

load_env()
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(os.environ["MTR_OP_HOST"], username="root", password=os.environ["MTR_OP_SSH_PASSWORD"], timeout=25, allow_agent=False, look_for_keys=False)
print(run(c, r"""
echo '=== ARP 249 row ==='
python3 <<'PY'
import sqlite3
c=sqlite3.connect('/root/mtr_op/data.db')
c.row_factory=sqlite3.Row
try:
    for r in c.execute("SELECT * FROM arp_spoof_targets WHERE spoof_gateway_ip LIKE '%249%' OR satellite_vrf LIKE '%43249%'"):
        print(dict(r))
except Exception as e:
    print('arp err', e)
try:
    for r in c.execute("SELECT * FROM bgp_neighbor_meta WHERE neighbor_ip='139.159.43.208' OR vrf LIKE '%43249%'"):
        print('meta', dict(r))
except Exception as e:
    print('meta err', e)
PY
echo '=== port for vbgp13915943249 ==='
python3 -c "vrf='vbgp13915943249';h=0
for ch in vrf: h=(h*31+ord(ch))&0xFFFF
print('port',1790+h)"
echo '=== nft 249 ==='
nft list table inet mtr_bgp_sat_dnat 2>/dev/null | grep 249 || echo MISSING_249_DNAT
echo '=== env DNAT ==='
grep -hE 'SAT_DNAT|IPVLAN|RR_UPLINK|DOWNSTREAM' /root/mtr_op/.env 2>/dev/null | head -10
systemctl show mtr-op.service -p Environment 2>/dev/null | tr ' ' '\n' | grep -E 'SAT_DNAT|IPVLAN|RR_UPLINK' | head -8
echo '=== POST reconcile dry? last log ==='
journalctl -u mtr-op --since '24 hours ago' --no-pager 2>/dev/null | grep -iE 'dnat|249|reconcile' | tail -15
"""))
c.close()
