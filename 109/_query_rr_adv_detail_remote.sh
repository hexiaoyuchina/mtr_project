#!/bin/bash
set +e
PY=python3
RR=139.159.43.249
DS=139.159.43.208
PEER207=139.159.43.207

echo "=== 1) OP RR 邻居行 ==="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors" | $PY <<'PY'
import json, sys
for n in json.load(sys.stdin):
    if n.get("vrf") == "gobgp-rr" and n.get("neighbor_ip") == "139.159.43.249":
        keys = [
            "vrf", "neighbor_ip", "remote_as", "source_ip", "role",
            "session_state", "routes_received", "routes_sent",
            "advertise_routes", "routes_cached",
        ]
        print(json.dumps({k: n.get(k) for k in keys}, ensure_ascii=False, indent=2))
        break
PY

echo ""
echo "=== 2) 通告任务状态 ==="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors/gobgp-rr/139.159.43.249/advertise/status" | $PY -m json.tool 2>/dev/null || echo "(no task / idle)"

echo ""
echo "=== 3) 聚合来源：下游邻居（source_ip=249）==="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors" | $PY <<'PY'
import json, sys
for n in json.load(sys.stdin):
    if str(n.get("role", "")).lower() == "downstream" and str(n.get("source_ip") or "") == "139.159.43.249":
        print(
            f"  vrf={n.get('vrf')} peer={n.get('neighbor_ip')} "
            f"session={n.get('session_state')} rcvd={n.get('routes_received')} "
            f"cached={n.get('routes_cached')}"
        )
PY

VRF=$(
  curl -sf "http://127.0.0.1:8808/api/bgp/neighbors" | $PY <<'PY'
import json, sys
for n in json.load(sys.stdin):
    if n.get("neighbor_ip") == "139.159.43.208" and str(n.get("role", "")).lower() == "downstream":
        print(n.get("vrf", ""))
        break
PY
)
echo ""
echo "=== 4) Agent 下游持久库（聚合读库源）vrf=${VRF:-?} peer=${DS} ==="
if [ -n "$VRF" ]; then
  echo -n "count: "
  curl -sf "http://127.0.0.1:9179/api/rib/routes/count?window=downstream&vrf=${VRF}&neighbor_ip=${DS}"
  echo ""
  echo "样本 prefix / nexthop（库内，发 RR 时会改为 ${PEER207}）:"
  curl -sf "http://127.0.0.1:9179/api/rib/routes?window=downstream&vrf=${VRF}&neighbor_ip=${DS}&page=1&page_size=20" | $PY <<'PY'
import json, sys
raw = sys.stdin.read()
if not raw.strip():
    print("  (empty API response)")
    sys.exit(0)
d = json.loads(raw)
items = d.get("items") or d.get("routes") or []
print("  total:", d.get("total", len(items)))
for x in items[:20]:
    p = x.get("prefix", "")
    nh = x.get("nexthop", "")
    asp = (x.get("as_path") or "")[:48]
    print(f"  {p:22s} nh={nh:16s} as_path={asp}")
PY
else
  echo "  (未找到下游 VRF)"
fi

echo ""
echo "=== 5) RX → ${RR} 实际发出（gobgp adj-out）==="
FOUND=0
for port in 50051 50052 17919; do
  if gobgp -p "$port" neighbor "$RR" adj-out 2>/dev/null | head -1 | grep -q .; then
    FOUND=1
    echo "gobgp -p $port"
    gobgp -p "$port" neighbor "$RR" 2>/dev/null | head -10
    CNT=$(gobgp -p "$port" neighbor "$RR" adj-out 2>/dev/null | wc -l)
    echo "adj-out 行数: $CNT"
    echo "样本（前 8 条）:"
    gobgp -p "$port" neighbor "$RR" adj-out 2>/dev/null | head -8
    echo "Next-Hop 分布（末列）:"
    gobgp -p "$port" neighbor "$RR" adj-out 2>/dev/null | awk '{print $NF}' | sort | uniq -c | sort -rn | head -8
    break
  fi
done
if [ "$FOUND" = 0 ]; then
  echo "  gobgp CLI 未找到 adj-out（可装 gobgp 或看 ROS 249 上 bgp route）"
fi

echo ""
echo "=== 6) 最近 RR 聚合通告日志 ==="
journalctl -u bgp-agent --since '2 hours ago' --no-pager 2>/dev/null | grep -iE 'rr-aggregate|rib job.*249|aggregate advertise' | tail -8
