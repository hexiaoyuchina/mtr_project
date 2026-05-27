#!/usr/bin/env python3
"""109：清理 main 表中 bgp-agent 误装的 upstream FIB 路由（保留 default/直连/管理路由）。"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script  # noqa: E402


def remote_script(dry_run: bool) -> str:
    mode = "check" if dry_run else "apply"
    return f"""
set -euo pipefail
MODE={mode!r}

echo "=== before ==="
echo -n "main total: "
ip route show table main | wc -l

TMP=$(mktemp)
ip route show table main | awk '
  / via / {{
    dst=$1
  if (dst == "default") next
  if (dst ~ /^127\\./) next
  if (dst ~ /^224\\./) next
  if (dst == "255.255.255.255") next
  if (dst ~ /^139\\.159\\.105\\./) next
  if (dst == "139.159.43.208/32") next
  if (dst == "139.159.43.209/32") next
  if (dst ~ /^local$|^broadcast$|^unreachable$|^blackhole$|^prohibit$|^throw$/) next
  print "route del " dst
  }}
' > "$TMP"

DEL=$(wc -l < "$TMP" | tr -d ' ')
echo "candidates_to_delete=$DEL"

if [[ "$MODE" == "check" ]]; then
  echo "=== sample delete lines ==="
  head -5 "$TMP" || true
  rm -f "$TMP"
  exit 0
fi

if [[ "$DEL" -eq 0 ]]; then
  rm -f "$TMP"
  echo "nothing to delete"
  exit 0
fi

echo "=== deleting via ip -batch (chunks of 5000) ==="
split -l 5000 "$TMP" "${{TMP}}."
for part in "${{TMP}}."*; do
  [[ -f "$part" ]] || continue
  ip -batch "$part"
done
rm -f "$TMP" "${{TMP}}."*
ip route flush cache 2>/dev/null || true

echo "=== after ==="
echo -n "main total: "
ip route show table main | wc -l
echo "=== remaining routes ==="
ip route show table main
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="清理 109 main 表误装 BGP FIB 路由")
    ap.add_argument("--apply", action="store_true", help="执行删除（默认仅 dry-run 统计）")
    args = ap.parse_args()
    load_env_file(ROOT / "109" / "env")
    load_env_file(ROOT / "109" / "env.example")
    host = os.environ.get("MTR_OP_HOST", "101.89.68.109")
    c = connect(host)
    code, out = run_script(c, remote_script(dry_run=not args.apply), timeout=900 if args.apply else 120)
    print(out)
    c.close()
    if code != 0:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
