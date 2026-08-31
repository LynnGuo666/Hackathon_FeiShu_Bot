#!/usr/bin/env python3
"""手动补拉：把「已验证 + 未入群」的选手批量拉入所有启用群。

用法：.venv/bin/python scripts/backfill.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.core.base_store import BaseStore  # noqa: E402
from bot.core.config import CFG  # noqa: E402
from bot.modules.group.group import pull_user_into_groups  # noqa: E402


def main() -> None:
    store = BaseStore(CFG.db_base_token)
    targets = []
    for r in store.list_records(CFG.tbl_contestants):
        f = r.get("fields") or {}
        if str(f.get("验证状态") or "") == "已验证" and str(f.get("入群状态") or "") != "已入群" and f.get("飞书open_id"):
            targets.append({"open_id": str(f["飞书open_id"]), "选手ID": str(f.get("选手ID") or ""),
                            "record_id": r["record_id"]})
    print(f"待补拉选手: {len(targets)}")
    if not targets:
        return
    ok, fail = pull_user_into_groups(targets)
    print(f"成功拉入群人次: {ok}")
    if fail:
        print(f"失败: {fail}")


if __name__ == "__main__":
    main()
