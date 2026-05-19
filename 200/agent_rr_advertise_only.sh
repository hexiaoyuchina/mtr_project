#!/bin/bash
# 在 200 上执行：仅向 Agent 提交 RR 聚合通告（不经过慢速 OP neighbors 列表）
set -e
RR=10.133.153.204
DB=/root/mtr_op/data/mtr.db
PEERS=$(python3 -c "
import json, sqlite3
RR='$RR'
conn=sqlite3.connect('$DB')
rows=conn.execute(
  'SELECT vrf,neighbor_ip FROM bgp_neighbor_meta WHERE source_ip=? AND neighbor_ip!=?',
  (RR,RR)).fetchall()
print(json.dumps([{'window':'downstream','vrf':v,'neighbor_ip':n} for v,n in rows]))
")
TASK="gobgp-rr-${RR}-advertise-$(date +%s)"
echo "peers=$(echo "$PEERS" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
curl -sf -X POST http://127.0.0.1:9179/api/rib/advertise \
  -H 'Content-Type: application/json' \
  -d "{\"task_id\":\"$TASK\",\"target\":\"rr\",\"enable\":true,\"src_peers\":$PEERS,\"batch_size\":5000}"
echo
echo "task_id=$TASK"
for i in $(seq 1 24); do
  st=$(curl -sf "http://127.0.0.1:9179/api/rib/advertise/status?task_id=$TASK" || echo '{}')
  echo "$i $st" | head -c 400
  echo
  echo "$st" | grep -q '"status":"completed"' && break
  echo "$st" | grep -q '"status":"error"' && break
  sleep 10
done
curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
import json,sys
for n in json.load(sys.stdin).get('neighbors',[]):
  if n.get('address')=='$RR':
    print('pfx_adv',n.get('pfx_adv'),'pfx_rcd',n.get('pfx_rcd'))
"
