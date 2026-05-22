#!/usr/bin/env python3
"""一次性：从 109/env 加载变量；缺二进制则本地编译，再 deploy_light。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "109" / "env"


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        key = k.strip()
        if key and key not in os.environ:
            os.environ[key] = v.strip()


def main() -> None:
    load_env(ENV_FILE)
    os.environ.setdefault("MTR_DEPLOY_BGP_AGENT_PREBUILT", "1")
    os.environ.setdefault("MTR_DEPLOY_BUILD_BGP_AGENT", "0")
    env = os.environ.copy()
    bin_path = ROOT / "service" / "bgp_agent" / "bgp_agent"
    steps: list[tuple[list[str], dict]] = []
    if not bin_path.is_file() or bin_path.stat().st_size == 0:
        steps.append(([sys.executable, str(ROOT / "tools" / "bgp_agent_build.py")], env))
    else:
        print(f"跳过编译，沿用已有二进制 ({bin_path.stat().st_size} bytes)", flush=True)
    steps.append(
        (
            [sys.executable, str(ROOT / "tools" / "deploy_light.py")],
            {**env, "MTR_DEPLOY_BGP_AGENT_LOCAL_BUILD": "0"},
        ),
    )
    for cmd, run_env in steps:
        print(">>>", " ".join(cmd), flush=True)
        r = subprocess.run(cmd, cwd=str(ROOT), env=run_env)
        if r.returncode != 0:
            raise SystemExit(r.returncode)


if __name__ == "__main__":
    main()
