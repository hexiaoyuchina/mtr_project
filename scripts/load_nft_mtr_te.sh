#!/bin/sh
# 加载 TE 改写 nft 占位表（不含 Echo NFQUEUE）
dir=$(dirname "$0")
nft -f "$dir/nft_mtr_te.nft" && nft list table inet mtr_te
