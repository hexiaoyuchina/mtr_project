#!/usr/bin/env python3
import sys
sys.path.insert(0, "/root/mtr_op")
from pathlib import Path
from app import storage

db = Path("/root/mtr_op/data.db")
conn = storage.connect(db)
storage.init_schema(conn)
ip = "10.133.152.233"
row = conn.execute(
    "SELECT id FROM arp_spoof_targets WHERE spoof_gateway_ip=?", (ip,)
).fetchone()
if row:
    storage.update_arp_spoof_target(
        conn,
        int(row["id"]),
        satellite_vrf="vbgp10133152233",
        egress_iface="ens192",
        enabled=True,
        note="lab233-test",
    )
    print("updated", row["id"])
else:
    conn.execute(
        """
        INSERT INTO arp_spoof_targets
        (spoof_gateway_ip, satellite_vrf, egress_iface, enabled, policy_mode, policy_cidrs, note, created_at)
        VALUES (?, ?, ?, 1, 'gateway_only', '', ?, datetime('now'))
        """,
        (ip, "vbgp10133152233", "ens192", "lab233-test"),
    )
    conn.commit()
    print("inserted", conn.execute("SELECT id FROM arp_spoof_targets WHERE spoof_gateway_ip=?", (ip,)).fetchone()[0])
conn.close()
