#!/bin/bash
set +e
RR=139.159.43.249
PFX=139.159.105.92
PEER=139.159.43.207

echo "=== Agent neighbor 249 (pfx adv?) ==="
curl -sf http://127.0.0.1:9179/api/neighbors -o /tmp/ag_nb.json
python3 <<'PY'
import json
d=json.load(open("/tmp/ag_nb.json"))
for n in d.get("neighbors") or []:
    if n.get("address") == "139.159.43.249":
        print(json.dumps(n, indent=2, ensure_ascii=False))
PY

echo ""
echo "=== RR advertise switch + task ==="
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors" -o /tmp/op_nb.json
python3 <<'PY'
import json
for n in json.load(open("/tmp/op_nb.json")):
    if n.get("neighbor_ip") == "139.159.43.249" and n.get("vrf") == "gobgp-rr":
        print("advertise_routes", n.get("advertise_routes"), "routes_sent", n.get("routes_sent"), "routes_received", n.get("routes_received"))
PY
curl -sf "http://127.0.0.1:8808/api/bgp/neighbors/gobgp-rr/139.159.43.249/advertise/status" -o /tmp/adv.json
cat /tmp/adv.json; echo

echo ""
echo "=== Downstream source (what we try to send) ==="
curl -sf "http://127.0.0.1:9179/api/rib/routes?window=downstream&vrf=vbgp13915943249&neighbor_ip=139.159.43.208&page=1&page_size=5" | python3 -m json.tool 2>/dev/null | head -40

echo ""
echo "=== BGP session to 249 ==="
ss -tn state established | grep -E '249:179|207:.*179' | head -5

echo ""
echo "=== gobgp: is prefix in global rib / adj-out to 249? ==="
if command -v gobgp >/dev/null 2>&1; then
  for p in 50051 50052; do
    echo "-- port $p --"
    gobgp -p "$p" neighbor "$RR" 2>/dev/null | head -12
    echo "adj-out grep $PFX:"
    gobgp -p "$p" neighbor "$RR" adj-out 2>/dev/null | grep -F "$PFX" || echo "(none)"
    echo "global rib:"
    gobgp -p "$p" global rib -a ipv4 "${PFX}/30" 2>&1 | head -5
  done
else
  echo "gobgp CLI not installed on 109"
fi

echo ""
echo "=== Is 105.92 learned FROM RR already? (same prefix loop) ==="
curl -sf "http://127.0.0.1:9179/api/rib/routes?window=downstream&vrf=vbgp13915943249&neighbor_ip=139.159.43.208&page=1&page_size=50" -o /tmp/ds.json
python3 <<'PY'
import json
d=json.load(open("/tmp/ds.json"))
print("downstream routes:", d.get("total"), "items", len(d.get("items") or []))
PY
