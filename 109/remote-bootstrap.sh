#!/bin/bash
# 现网 VR：安装系统依赖；已满足则 SKIP（与 service/scripts/deploy_bgp_rxtx.py 对齐）
set -e
export DEBIAN_FRONTEND=noninteractive
REMOTE="${MTR_OP_REMOTE_DIR:-/root/mtr_op}"

apt_install() {
  local pkg="$1"
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    echo "SKIP apt: $pkg"
  else
    echo "INSTALL apt: $pkg"
    apt-get install -y -qq "$pkg"
  fi
}

if systemctl is-active redis-server >/dev/null 2>&1 && redis-cli ping 2>/dev/null | grep -q PONG; then
  echo "SKIP redis-server (already OK)"
else
  echo "INSTALL redis-server..."
  apt-get update -qq
  apt-get install -y -qq redis-server
  systemctl enable redis-server
  systemctl start redis-server
  redis-cli ping
fi

apt-get update -qq
for pkg in librocksdb-dev g++ build-essential pkg-config python3-venv python3-pip \
  nftables python3-scapy python3-dev libnetfilter-queue-dev iptables iproute2 \
  mtr traceroute ca-certificates curl wget; do
  apt_install "$pkg"
done

export PATH=/usr/local/go/bin:$PATH
if command -v go >/dev/null 2>&1 && go version | grep -qE 'go1\.(2[1-9]|[3-9])'; then
  echo "SKIP Go: $(go version)"
else
  echo "INSTALL Go 1.21.13..."
  export GOPROXY=https://goproxy.cn,direct
  export GOSUMDB=sum.golang.google.cn
  cd /tmp
  curl -fsSL -o go.tar.gz https://go.dev/dl/go1.21.13.linux-amd64.tar.gz
  rm -rf /usr/local/go
  tar -C /usr/local -xzf go.tar.gz
  rm -f go.tar.gz
  export PATH=/usr/local/go/bin:$PATH
  go version
fi

mkdir -p "$REMOTE"
mkdir -p "$(dirname "${ROCKSDB_PATH:-/var/lib/bgp_agent/rocksdb}")"

echo "remote-bootstrap_ok"
