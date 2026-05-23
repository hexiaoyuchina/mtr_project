#!/usr/bin/env python3
"""
现网 109：管理面 SSH(22) 与 OP Web(8808) 源 IP 白名单（nft inet mtr_admin_acl）。

- 默认允许网段见 DEFAULT_CIDRS / env MTR_ADMIN_ACL_CIDRS（逗号分隔）
- 本机 127.0.0.0/8、已建立连接放行；其它访问 22/8808 丢弃
- uvicorn 监听 0.0.0.0 时，对所有入口生效（不仅管理口）

用法：
  python 109/apply_admin_acl.py
  python 109/apply_admin_acl.py --check
  python 109/apply_admin_acl.py --teardown

文档：docs/ADMIN_ACL_109.md
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

DIR = Path(__file__).resolve().parent
ROOT = DIR.parent

DEFAULT_CIDRS = (
    "101.251.211.168/29",
    "101.251.214.176/28",
    "106.120.247.120/29",
    "101.251.204.16/29",
    "164.52.12.80/29",
    "101.251.255.176/29",
)


def load_env() -> None:
    for name in ("env", "env.example"):
        p = DIR / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
        break


def parse_cidrs() -> list[str]:
    raw = os.environ.get("MTR_ADMIN_ACL_CIDRS", "").strip()
    if raw:
        parts = [x.strip() for x in raw.replace(" ", ",").split(",") if x.strip()]
    else:
        parts = list(DEFAULT_CIDRS)
    return parts


def nft_elements_lines(cidrs: list[str]) -> str:
    lines = []
    for i, c in enumerate(cidrs):
        sep = "," if i < len(cidrs) - 1 else ""
        lines.append(f"      {c}{sep}")
    return "\n".join(lines)


def remote_script(mode: str) -> str:
    cidrs = parse_cidrs()
    elements = nft_elements_lines(cidrs)
    remote_dir = os.environ.get("MTR_OP_REMOTE_DIR", "/root/mtr_op").strip()
    nft_path = os.environ.get("MTR_ADMIN_ACL_NFT", f"{remote_dir}/nft_mtr_admin_acl.nft").strip()
    persist = os.environ.get(
        "MTR_ADMIN_ACL_PERSIST",
        "/usr/local/sbin/mtr-admin-acl.sh",
    ).strip()
    op_port = os.environ.get("MTR_OP_PORT", "8808").strip()

    nft_body = f"""table inet mtr_admin_acl {{
  set admin_whitelist {{
    type ipv4_addr
    flags interval
    elements = {{
{elements}
    }}
  }}

  chain input {{
    type filter hook input priority filter - 15; policy accept;
    iif "lo" accept
    ct state established,related accept
    tcp dport {{ 22, {op_port} }} ip saddr 127.0.0.0/8 accept
    tcp dport {{ 22, {op_port} }} ip saddr @admin_whitelist accept
    tcp dport {{ 22, {op_port} }} counter drop
  }}
}}
"""

    return f"""
set -e
MODE={mode!r}
NFT_PATH={nft_path!r}
PERSIST={persist!r}

teardown() {{
  nft delete table inet mtr_admin_acl 2>/dev/null || true
  echo "teardown mtr_admin_acl done"
}}

apply() {{
  mkdir -p "$(dirname "$NFT_PATH")"
  cat > "$NFT_PATH" <<'NFTEOF'
{nft_body}
NFTEOF
  nft delete table inet mtr_admin_acl 2>/dev/null || true
  nft -f "$NFT_PATH"
  cat > "$PERSIST" <<'PEOF'
#!/bin/bash
set -e
NFT=__NFT_PATH__
[ -f "$NFT" ] || exit 0
nft delete table inet mtr_admin_acl 2>/dev/null || true
nft -f "$NFT"
PEOF
  sed -i "s|__NFT_PATH__|$NFT_PATH|g" "$PERSIST"
  chmod +x "$PERSIST"
  echo "applied: $NFT_PATH"
}}

verify() {{
  echo "=== nft table mtr_admin_acl ==="
  nft list table inet mtr_admin_acl 2>/dev/null || echo "MISSING"
  echo "=== counters (drop hits) ==="
  nft list chain inet mtr_admin_acl input 2>/dev/null | tail -5 || true
  echo "=== persist script ==="
  ls -la "$PERSIST" 2>/dev/null || true
}}

case "$MODE" in
  teardown) teardown ;;
  check) verify ;;
  *) apply; verify ;;
esac
"""


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description="109 管理面 SSH/OP 白名单")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--teardown", action="store_true")
    args = ap.parse_args()
    mode = "teardown" if args.teardown else ("check" if args.check else "apply")

    host = os.environ.get("MTR_OP_HOST", "101.89.68.109").strip()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not pw:
        print("请设置 MTR_OP_SSH_PASSWORD（109/env）", file=sys.stderr)
        raise SystemExit(2)
    user = os.environ.get("MTR_OP_SSH_USER", "root").strip()

    print(f"Connecting {user}@{host} mode={mode} ...", flush=True)
    print("CIDRs:", ", ".join(parse_cidrs()), flush=True)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        host,
        username=user,
        password=pw,
        timeout=30,
        banner_timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    stdin, stdout, stderr = c.exec_command("bash -se", timeout=120)
    stdin.write(remote_script(mode))
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    c.close()
    print(out, end="")
    if err.strip():
        print(err, file=sys.stderr, end="")
    if code != 0:
        raise SystemExit(code)
    if mode == "apply":
        print(
            "WARN: 请确认当前办公网 IP 落在白名单内，否则 SSH/8808 将被拒。",
            flush=True,
        )


if __name__ == "__main__":
    main()
