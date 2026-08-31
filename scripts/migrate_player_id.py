#!/usr/bin/env python3
"""一次性迁移：选手表旧格式 选手ID（WY-0001）改写为新格式（WY01-0001），序号不变。

用法：
  .venv/bin/python scripts/migrate_player_id.py --dry-run   # 只预览，不写入
  .venv/bin/python scripts/migrate_player_id.py             # 执行改写
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.core.base_store import BaseStore  # noqa: E402
from bot.core.config import CFG  # noqa: E402
from bot.modules.sync.sync import _text  # noqa: E402

OLD_RE = re.compile(r"^WY-(\d+)$")


def main() -> None:
    dry = "--dry-run" in sys.argv
    store = BaseStore(CFG.db_base_token)
    updates = []
    for r in store.list_records(CFG.tbl_contestants):
        f = r.get("fields") or {}
        cid = _text(f.get("选手ID"))
        m = OLD_RE.match(cid)
        if m:
            updates.append({"record_id": r["record_id"], "_old": cid,
                            "fields": {"选手ID": f"WY01-{int(m.group(1)):04d}"}})
    print(f"待改写记录: {len(updates)}")
    for u in updates:
        print(f"  {u['_old']} -> {u['fields']['选手ID']}")
    if not updates:
        return
    if dry:
        print("dry-run，未写入。")
        return
    store.batch_update(CFG.tbl_contestants,
                       [{"record_id": u["record_id"], "fields": u["fields"]} for u in updates])
    print(f"已改写 {len(updates)} 条。")


if __name__ == "__main__":
    main()
