#!/bin/sh
# 卸载 MTR 伪造 nft 表（Linux 200）
nft delete table inet mtr_spoof 2>/dev/null && echo "removed inet mtr_spoof" || echo "table absent or error"
