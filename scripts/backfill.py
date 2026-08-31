#!/usr/bin/env python3
"""手动补拉：把所有已验证选手补进缺失的群（以群成员实时数据为准，含新增群）。

用法：.venv/bin/python scripts/backfill.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.core.base_store import BaseStore  # noqa: E402
from bot.core.config import CFG  # noqa: E402
from bot.modules.group.group import pull_user_into_groups, verified_users  # noqa: E402


def main() -> None:
    store = BaseStore(CFG.db_base_token)
    targets = verified_users(store)
    print(f"已验证选手: {len(targets)}")
    if not targets:
        return
    ok, fail = pull_user_into_groups(targets)
    print(f"本次补拉入群人次: {ok}")
    if fail:
        print(f"失败: {fail}")


if __name__ == "__main__":
    main()
