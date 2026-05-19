#!/usr/bin/env python3
"""手动刷新 201 TE 改写（日常请依赖 OP hop 规则 API 自动 sync；本脚本作排障用）。"""
from __future__ import annotations

import sys

from setup_201_te_rewrite_input import main

if __name__ == "__main__":
    raise SystemExit(main())
