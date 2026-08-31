#!/usr/bin/env python3
"""一次性初始化：全量同步报名表 -> 选手数据库（建表已由 lark-cli 完成）。

用法：.venv/bin/python scripts/init_base.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.modules.sync.sync import run_sync  # noqa: E402

if __name__ == "__main__":
    stats = run_sync()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
