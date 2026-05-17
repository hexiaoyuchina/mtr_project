#!/usr/bin/env python3
"""检查 main.py 等对 storage 的调用是否在 storage 模块中存在。"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "service" / "app"


def storage_funcs() -> set[str]:
    text = (ROOT / "storage.py").read_text(encoding="utf-8")
    return {m.group(1) for m in re.finditer(r"^def (\w+)", text, re.M)}


def find_storage_calls(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in re.finditer(r"storage\.(\w+)\s*\(", line):
            out.append((i, m.group(1)))
    return out


def main() -> int:
    funcs = storage_funcs()
    missing: list[str] = []
    for py in sorted(ROOT.glob("*.py")):
        if py.name == "storage.py":
            continue
        for ln, name in find_storage_calls(py):
            if name not in funcs:
                missing.append(f"{py.name}:{ln}: storage.{name}")
    if missing:
        print("MISSING storage functions:")
        for x in missing:
            print(" ", x)
        return 1
    print("All storage.* calls in app/*.py match storage.py definitions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
