#!/bin/bash
PID=$(pgrep -f '/bgp_agent' | head -1)
echo "strace peer add PID=$PID"
timeout 8 strace -f -e trace=connect,socket,bind -p "$PID" 2>&1 &
ST=$!
sleep 1
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"address":"139.159.43.249","remote_as":63199,"local_address":"139.159.43.207"}' \
  http://127.0.0.1:9179/api/rr/config
sleep 6
kill $ST 2>/dev/null || true
wait $ST 2>/dev/null
