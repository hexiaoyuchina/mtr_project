#!/usr/bin/env python3
"""
Linux 200 实验室部署：上传 service/ + overlay，保留 data.db，重启 bgp-agent / mtr-op。

不修改仓库 tools/、service/ 原文件；复用 tools/deploy_light 的上传逻辑与 tools/bgp_agent_remote。

用法（仓库根目录）：
  pip install paramiko
  python 200/deploy.py
"""
from __future__ import annotations

import os
import posixpath
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2)

LAB_DIR = Path(__file__).resolve().parent
ROOT = LAB_DIR.parent
sys.path.insert(0, str(ROOT / "tools"))

from bgp_agent_remote import bgp_agent_config_from_env, shell_sync_bgp_agent  # noqa: E402
from deploy_light import (  # noqa: E402
    REMOTE,
    SKIP_DIRS,
    connect,
    run_script,
    upload_bgp_agent_tree,
    upload_tree,
)

SERVICE = ROOT / "service"
NFQ = ROOT / "scripts" / "mtr_spoof_nfqueue.py"
OVERLAY = LAB_DIR / "overlay" / "bgp_agent"


def load_lab_env() -> None:
    env_file = LAB_DIR / "lab.env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()


def apply_overlay(sftp: paramiko.SFTPClient, remote_bgp: str) -> None:
    if not OVERLAY.is_dir():
        return

    def ensure_dir(path: str) -> None:
        if not path or path == "/":
            return
        try:
            sftp.stat(path)
        except OSError:
            ensure_dir(posixpath.dirname(path))
            try:
                sftp.mkdir(path)
            except OSError:
                pass

    for p in OVERLAY.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(OVERLAY).as_posix()
        rp = posixpath.join(remote_bgp, rel)
        ensure_dir(posixpath.dirname(rp))
        sftp.put(str(p), rp)
        print(f"  overlay -> {rp}")


def main() -> None:
    load_lab_env()
    host = os.environ.get("MTR_OP_HOST", "10.133.151.200").strip()
    pw = os.environ.get("MTR_OP_SSH_PASSWORD", "").strip()
    if not pw:
        print("请设置 MTR_OP_SSH_PASSWORD（或写入 200/lab.env）", file=sys.stderr)
        sys.exit(2)
    if not SERVICE.is_dir() or not NFQ.is_file():
        print("缺少 service/ 或 scripts/mtr_spoof_nfqueue.py", file=sys.stderr)
        sys.exit(1)

    remote = os.environ.get("MTR_OP_REMOTE_DIR", REMOTE).strip()
    print(f"=== Linux 200 部署 -> {host}:{remote} ===")
    print(f"    AS={os.environ.get('LOCAL_AS', '63199')} ROUTER_ID={os.environ.get('ROUTER_ID', '')}")

    c = connect(host)
    sftp = c.open_sftp()
    try:
        upload_tree(sftp, SERVICE, remote)
        sftp.put(str(NFQ), f"{remote}/mtr_spoof_nfqueue.py")
        print("=== overlay（仅覆盖实验室补丁文件）===")
        apply_overlay(sftp, posixpath.join(remote, "bgp_agent"))
        for name in ("remote-restart.sh", "remote-network-prereq.sh"):
            lp = LAB_DIR / name
            if lp.is_file():
                sftp.put(str(lp), f"{remote}/{name}")
    finally:
        sftp.close()

    net = f"bash {remote}/remote-network-prereq.sh 2>/dev/null || true"
    code, out = run_script(c, net, timeout=30)
    print(out, end="")

    restart = f"export MTR_OP_REMOTE_DIR={remote}\n"
    for key in (
        "LOCAL_AS",
        "ROUTER_ID",
        "MTR_DOWNSTREAM_REMOTE_AS",
        "MTR_BGP_IPVLAN_AUTO",
        "MTR_BGP_IPVLAN_BASE_IFACE",
        "MTR_BGP_RR_UPLINK_IFACE",
        "MTR_BGP_IPVLAN_PEER_IP",
        "MTR_SATELLITE_PEER_IP",
        "MTR_SATELLITE_PHY_VRF",
        "MTR_AUTO_SATELLITE_VRF",
        "MTR_AUTO_SATELLITE_VRF_NOTE",
        "MTR_BGP_RIB_SYNC",
        "MTR_BGP_RIB_SYNC_SEC",
        "MTR_PROBE_SSH_HOST",
        "MTR_BGP_ROLE_MAP",
        "MTR_BGP_DB_PRESETS",
    ):
        val = os.environ.get(key, "")
        if val:
            restart += f'export {key}="{val}"\n'
    restart += f"bash {remote}/remote-restart.sh\n"

    rebuild = os.environ.get("MTR_DEPLOY_BUILD_BGP_AGENT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    bgp_cfg = bgp_agent_config_from_env()
    bgp_cfg["remote_dir"] = remote
    restart += shell_sync_bgp_agent(bgp_cfg, rebuild=rebuild)

    post = f"""
python3 - <<'PY'
import json, urllib.request, time
AS=int("{os.environ.get("LOCAL_AS", "63199")}")
agent="http://127.0.0.1:9179"
base="http://127.0.0.1:8808"

def req(m,u,b=None):
 d=json.dumps(b).encode() if b else None
 r=urllib.request.Request(u,data=d,method=m,headers={{"Content-Type":"application/json"}} if d else {{}})
 return urllib.request.urlopen(r,timeout=60).read().decode()

print("rr", req("POST", agent+"/api/rr/config", {{
  "address":"10.133.153.204","remote_as":AS,"local_address":"10.133.153.200"}}))
try:
 print("sync", req("POST", base+"/api/bgp/sync-from-frr"))
except Exception as e:
 print("sync warn", e)
try:
 print("down", req("PATCH", base+"/api/bgp/neighbors/default/10.133.152.204", {{
  "remote_as":AS,"local_as":AS,"source_ip":"10.133.152.200","role":"downstream"}}))
except Exception as e:
 print("down warn", e)
time.sleep(4)
print("freeze", req("GET", agent+"/api/peers/freeze-status"))
PY
"""
    restart += post

    print("=== 重启服务 ===")
    code, out = run_script(c, restart, timeout=600)
    print(out, end="")
    c.close()
    if code != 0:
        print(f"deploy exit={code}", file=sys.stderr)
        sys.exit(code)
    print("deploy_200_ok — 校验: python 200/verify.py")


if __name__ == "__main__":
    main()
