#!/usr/bin/env python3
"""重触发 gobgp-rr 聚合通告，并打印库/会话计数对比。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
RR = "10.133.153.204"


def load_env() -> str:
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    return os.environ.get("MTR_OP_SSH_PASSWORD", "1234qwer")


def root(pw: str, script: str, timeout: int = 300) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(os.environ.get("MTR_OP_HOST", "10.133.151.200"), username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=timeout)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    return (o.read() + e.read()).decode("utf-8", "replace")


def main() -> int:
    pw = load_env()
    print(
        root(
            pw,
            f"""
set -e
RR={RR}
echo '--- agent rr peer ---'
curl -s --max-time 30 http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='{RR}':
    print('pfx_rcd',n.get('pfx_rcd'),'pfx_adv',n.get('pfx_adv'),'state',n.get('state'))
"
echo '--- sqlite downstream ribs ---'
python3 -c "
import sqlite3, urllib.parse, urllib.request, json
RR='{RR}'
db='/root/mtr_op/data/mtr.db'
conn=sqlite3.connect(db)
rows=conn.execute(
  'SELECT vrf,neighbor_ip,advertise_routes FROM bgp_neighbor_meta WHERE source_ip=? AND neighbor_ip!=?',
  (RR,RR)).fetchall()
print('downstream_peers',len(rows))
total=0
peers=[]
for vrf,nip,ar in rows:
  q=urllib.parse.urlencode({{'window':'downstream','vrf':vrf,'neighbor_ip':nip}})
  try:
    with urllib.request.urlopen('http://127.0.0.1:9179/api/rib/routes/count?'+q, timeout=60) as r:
      c=int(json.load(r).get('count') or 0)
  except Exception as ex:
    c=-1
    print('count_err',vrf,nip,ex)
  total+=max(c,0)
  peers.append({{'window':'downstream','vrf':vrf,'neighbor_ip':nip}})
  print(vrf,nip,'ar',ar,'rib',c)
print('sum_rib_lines',total)
open('/tmp/rr_agg_peers.json','w').write(json.dumps(peers))
"
echo '--- trigger rib advertise (rr aggregate) ---'
TASK_ID="gobgp-rr-${{RR}}-advertise-retry-$(date +%s)"
PEERS=$(cat /tmp/rr_agg_peers.json)
curl -s --max-time 30 -X POST http://127.0.0.1:9179/api/rib/advertise \\
  -H 'Content-Type: application/json' \\
  -d "{{\\"task_id\\":\\"$TASK_ID\\",\\"target\\":\\"rr\\",\\"src_peers\\":$PEERS,\\"enable\\":true,\\"batch_size\\":5000}}"
echo
echo task=$TASK_ID
for i in $(seq 1 120); do
  st=$(curl -s --max-time 15 "http://127.0.0.1:9179/api/rib/advertise/status?task_id=$TASK_ID" || echo '{{}}')
  echo "$i $st" | head -c 500
  echo
  echo "$st" | python3 -c "import json,sys; j=json.load(sys.stdin); s=j.get('status',''); sys.exit(0 if s in ('completed','error') else 1)" 2>/dev/null && break
  sleep 5
done
echo '--- after ---'
curl -s --max-time 30 http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='{RR}':
    print(json.dumps(n,indent=2))
"
""",
            timeout=900,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
