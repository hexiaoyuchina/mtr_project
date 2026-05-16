#!/bin/sh
# 加载 NFQUEUE 规则（Linux 200，需 nft）
dir=$(dirname "$0")
nft -f "$dir/nft_mtr_spoof.nft" && nft list table inet mtr_spoof
