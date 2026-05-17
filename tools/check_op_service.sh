#!/bin/bash
echo "=== mtr-op status ==="
systemctl is-active mtr-op 2>/dev/null || true
systemctl status mtr-op --no-pager -n 20 2>&1 | tail -22
echo "=== health ==="
curl -sf -m5 http://127.0.0.1:8808/health || echo "health_FAIL"
echo
curl -sf -m5 http://127.0.0.1:8808/ | head -c 200 || echo "index_FAIL"
echo
echo "=== listen ==="
ss -ltnp 2>/dev/null | grep 8808 || true
ss -ltnp 2>/dev/null | grep 9179 || true
echo "=== journal mtr-op ==="
journalctl -u mtr-op -n 40 --no-pager 2>&1 | tail -35
echo "=== python import ==="
cd /root/mtr_op 2>/dev/null && ./venv/bin/python -c "from app import main; print('import_ok')" 2>&1 || echo import_fail
