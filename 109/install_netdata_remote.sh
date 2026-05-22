#!/usr/bin/env bash
# 在 109 后台安装 Netdata + 1 天保留；状态见 /root/netdata_install.log
set -euo pipefail
LOG=/root/netdata_install.log
FLAG=/root/netdata_install.done
CONF=/etc/netdata/conf.d/mtr-retention-1d.conf
KICKSTART=/tmp/netdata-kickstart.sh

exec >>"$LOG" 2>&1
echo "=== start $(date -Is) ==="

if command -v netdata >/dev/null 2>&1; then
  echo "netdata already present"
else
  curl -fsSL https://get.netdata.cloud/kickstart.sh -o "$KICKSTART"
  chmod +x "$KICKSTART"
  sh "$KICKSTART" --non-interactive --stable-channel --disable-telemetry
fi

mkdir -p /etc/netdata/conf.d
cat >"$CONF" <<'EOF'
# MTR 109：Netdata 本地历史约保留 1 天（dbengine tier 0）
[db]
    mode = dbengine
    storage tiers = 1
    dbengine tier 0 retention time = 1d
    dbengine tier 0 retention size = 2GiB
EOF

PUBLIC=/etc/netdata/conf.d/mtr-public-web.conf
cat >"$PUBLIC" <<'EOF'
# MTR 109：Web 监听所有接口，允许公网访问（请配合云安全组放行 19999/tcp）
[web]
    bind to = *
    allow connections from = *
EOF

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q 'Status: active'; then
  ufw allow 19999/tcp comment 'netdata' || true
fi
if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active firewalld >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=19999/tcp || true
  firewall-cmd --reload || true
fi
iptables -C INPUT -p tcp --dport 19999 -j ACCEPT 2>/dev/null \
  || iptables -I INPUT -p tcp --dport 19999 -j ACCEPT 2>/dev/null || true

systemctl enable netdata
systemctl restart netdata
sleep 3
systemctl is-active netdata
netdata -v 2>&1 | head -1 || true
curl -fsS http://127.0.0.1:19999/api/v1/info | head -c 300 || true
echo
ss -lntp | grep 19999 || true

date -Is >"$FLAG"
echo "=== done $(date -Is) ==="
