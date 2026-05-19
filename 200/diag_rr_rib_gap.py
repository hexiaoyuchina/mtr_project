#!/usr/bin/env python3
"""排查 gobgp-rr Received vs 持久库条数差异。"""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import paramiko

LAB = Path(__file__).resolve().parent
RR = "10.133.153.204"
VRF = "gobgp-rr"


def load_env() -> tuple[str, str]:
    pw = "1234qwer"
    host = "10.133.151.200"
    for line in (LAB / "lab.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    return os.environ.get("MTR_OP_SSH_PASSWORD", pw), os.environ.get("MTR_OP_HOST", host)


def main() -> int:
    pw, host = load_env()
    remote = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op")
    script = textwrap.dedent(
        f"""
        set -e
        export REMOTE={remote}
        echo '=== 1. OP 邻居行 ==='
        curl -sf http://127.0.0.1:8808/api/bgp/neighbors | python3 -c "
        import json,sys
        for r in json.load(sys.stdin):
          if r.get('neighbor_ip')=='{RR}':
            print(json.dumps(r, indent=2, ensure_ascii=False))
        "

        echo
        echo '=== 2. Agent RR status / neighbors ==='
        curl -sf http://127.0.0.1:9179/api/rr/status | python3 -m json.tool 2>/dev/null | head -35
        echo '---'
        curl -sf http://127.0.0.1:9179/api/neighbors | python3 -c "
        import json,sys
        for n in json.load(sys.stdin).get('neighbors',[]):
          if n.get('address')=='{RR}':
            print(json.dumps(n, indent=2))
        "

        echo
        echo '=== 3. RIB count (upstream) ==='
        curl -sf 'http://127.0.0.1:9179/api/rib/routes/count?window=upstream&vrf={VRF}&neighbor_ip={RR}' | python3 -m json.tool

        echo
        echo '=== 4. SQLite meta / snapshot ==='
        $REMOTE/venv/bin/python3 - <<'PY'
        import sqlite3, os
        db = os.environ.get("REMOTE", "/root/mtr_op") + "/data.db"
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT vrf, neighbor_ip, role, store_received_routes, advertise_routes FROM bgp_neighbor_meta WHERE neighbor_ip=?",
            ("{RR}",),
        ):
            print("meta:", dict(row))
        for row in conn.execute(
            "SELECT vrf, neighbor_ip, frozen, route_count, session_established, last_sync_at FROM bgp_peer_snapshot WHERE neighbor_ip=?",
            ("{RR}",),
        ):
            print("snapshot:", dict(row))
        n = conn.execute(
            "SELECT COUNT(*) FROM bgp_learned_routes WHERE neighbor_ip=?", ("{RR}",)
        ).fetchone()[0]
        print("sqlite bgp_learned_routes rows for RR ip:", n)
        conn.close()
        PY

        echo
        echo '=== 5. Redis policy + counter ==='
        redis-cli GET 'peer:policy:{VRF}:{RR}' 2>/dev/null || echo 'no policy key'
        redis-cli GET 'rib:cnt:upstream:{VRF}:{RR}' 2>/dev/null || echo 'no cnt key'
        echo -n 'redis keys sample: '
        redis-cli --scan --pattern 'rib:upstream:{VRF}:{RR}:*' 2>/dev/null | head -3 | wc -l
        redis-cli --scan --pattern 'rib:upstream:{VRF}:{RR}:*' 2>/dev/null | wc -l

        echo
        echo '=== 6. freeze-status ==='
        curl -sf http://127.0.0.1:9179/api/peers/freeze-status | python3 -c "
        import json,sys
        d=json.load(sys.stdin)
        for u in d.get('upstream',[]):
          if u.get('neighbor_ip')=='{RR}':
            print('upstream freeze row:', u)
        print('upstream_any_up:', d.get('upstream_any_up'))
        "

        echo
        echo '=== 7. 试 ingest（只读对比前先看 policy）==='
        POL=$(redis-cli GET 'peer:policy:{VRF}:{RR}' 2>/dev/null || true)
        echo "policy raw: $POL"
        echo 'ingest (may take minutes)...'
        curl -s -w '\\nHTTP:%{{http_code}} time:%{{time_total}}s\\n' -X POST \\
          'http://127.0.0.1:9179/api/rib/ingest-peer?window=upstream&vrf={VRF}&neighbor_ip={RR}' \\
          --max-time 600 | tee /tmp/ingest_rr.json
        echo
        cat /tmp/ingest_rr.json | python3 -m json.tool 2>/dev/null || cat /tmp/ingest_rr.json

        echo
        echo '=== 8. ingest 后 count ==='
        curl -sf 'http://127.0.0.1:9179/api/rib/routes/count?window=upstream&vrf={VRF}&neighbor_ip={RR}' | python3 -m json.tool
        curl -sf http://127.0.0.1:8808/api/bgp/neighbors | python3 -c "
        import json,sys
        for r in json.load(sys.stdin):
          if r.get('neighbor_ip')=='{RR}':
            print('OP after ingest:', r.get('routes_received'), '/', r.get('routes_cached'))
        "

        echo
        echo '=== 9. agent journal (ingest/peer/ignore) ==='
        journalctl -u bgp-agent -n 200 --no-pager 2>/dev/null | grep -iE 'ingest|153.204|ignore|RR断|freeze|peer withdraw|Peer Up|store' | tail -40
        """
    )
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=30, allow_agent=False, look_for_keys=False)
    _, o, e = c.exec_command("bash -se", timeout=660)
    o.channel.send(script.encode())
    o.channel.shutdown_write()
    out = (o.read() + e.read()).decode("utf-8", "replace")
    c.close()
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
