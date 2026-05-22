#!/bin/sh
nft delete table inet mtr_te 2>/dev/null && echo "removed inet mtr_te" || echo "table absent or error"
nft delete table ip mtr_te_snat 2>/dev/null && echo "removed ip mtr_te_snat" || true
