#!/bin/bash
set -e
# 删除误伤 RR 的策略路由
while ip -4 rule show | grep -q 'from 139.159.43.207 lookup'; do
  pref=$(ip -4 rule show | awk '/from 139.159.43.207 lookup/{print $1}' | tr -d : | head -1)
  [ -n "$pref" ] && ip -4 rule del pref "$pref" || break
done
ip route flush cache
echo "=== route after del rule ==="
ip route get 139.159.43.249 from 139.159.43.207
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"address":"139.159.43.249","remote_as":63199,"local_address":"139.159.43.207"}' \
  http://127.0.0.1:9179/api/rr/config
echo ""
sleep 10
curl -sf http://127.0.0.1:9179/api/neighbors
echo ""
ss -tn | grep '249.*179\|179.*249' || true
