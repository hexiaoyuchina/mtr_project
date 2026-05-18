#!/usr/bin/env python3
import json, os, time, urllib.request
from pathlib import Path

for line in (Path(__file__).parent / "lab.env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()

base = f"http://{os.environ.get('MTR_OP_HOST','10.133.151.200')}:{os.environ.get('MTR_OP_PORT','8808')}"
VRF, PEER = "vbgp10133153204", "10.133.152.204"


def post(path, body):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def get_neighbors():
    with urllib.request.urlopen(base + "/api/bgp/neighbors", timeout=30) as r:
        return json.loads(r.read())


for en in (False, True):
    print("toggle", en, "->", post(f"/api/bgp/neighbors/{VRF}/{PEER}/toggle", {"enabled": en}))
    time.sleep(8)

row = next(r for r in get_neighbors() if r["vrf"] == VRF and r["neighbor_ip"] == PEER)
print("final", row)
agent = json.loads(urllib.request.urlopen("http://10.133.151.200:9179/api/neighbors").read())
n = next(x for x in agent["neighbors"] if x.get("vrf") == VRF)
print("agent", n)
ok = row.get("session_state") == "Established" and int(n.get("remote_as") or 0) == 63199
print("PASS" if ok else "FAIL")
