#!/bin/bash
echo '--- listen 179 ---'
ss -ltnp | grep 179
echo '--- lsof bgp ---'
pid=$(pgrep -f bgp_agent | head -1)
echo pid=$pid
ss -ltnp | grep "$pid" || true
# test if sport 179 connect works
python3 - <<'PY'
import socket
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(('139.159.43.207', 179))
    print('bind 207:179 OK')
except OSError as e:
    print('bind 207:179 FAIL', e)
s.close()
PY
