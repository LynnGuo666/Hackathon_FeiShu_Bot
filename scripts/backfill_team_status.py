#!/usr/bin/env python3
"""回填历史缺失的状态数据（表单变更前创建的记录）。

范围（只补空缺，不覆盖已有值）：
1. 队伍表：`是否预组队` / `同意统一分配` 为空的行 —— 从报名表读回；
   报名表也没有值时按同步器创建路径的默认值回填（预组队=是、同意分配=否）。
2. 选手表：`报名记录ID` 为空的行 —— 按手机号从报名表找回（主报名人优先）。

用法：
  .venv/bin/python scripts/backfill_team_status.py            # 干跑，只打印计划
  .venv/bin/python scripts/backfill_team_status.py --apply    # 实际写入
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.core.base_store import BaseStore  # noqa: E402
from bot.core.config import CFG  # noqa: E402
from bot.modules.sync.sync import (  # noqa: E402
    ROLE_FIELD_PREFIX, ROLES, extract_appearances, normalize_phone)

APPLY = "--apply" in sys.argv


def load(store: BaseStore, table: str) -> list[dict]:
    """bitable 副本偶发 1254607 Data not ready，重试几次。"""
    for i in range(5):
        try:
            return store.list_records(table)
        except RuntimeError as e:
            if "1254607" in str(e) and i < 4:
                time.sleep(2 * (i + 1))
                continue
            raise
    return []


def select_one(cell) -> str:
    if isinstance(cell, list):
        return str(cell[0]).strip() if cell else ""
    return str(cell or "").strip()


def link_text(cell) -> str:
    if isinstance(cell, list) and cell and isinstance(cell[0], dict):
        return str(cell[0].get("text", "")).strip()
    return str(cell or "").strip()


def main() -> None:
    reg_store = BaseStore(CFG.reg_base_token)
    db_store = BaseStore(CFG.db_base_token)

    regs = load(reg_store, CFG.reg_table_id)
    teams = load(db_store, CFG.tbl_teams)
    contestants = load(db_store, CFG.tbl_contestants)
    print(f"报名表 {len(regs)} 条, 队伍表 {len(teams)} 行, 选手表 {len(contestants)} 行")

    reg_by_rid = {r["record_id"]: (r.get("fields") or {}) for r in regs}

    # 报名表合并视图：phone -> {reg_record_id, is_owner}（主报名人优先）
    phone_reg: dict[str, tuple[str, bool]] = {}
    for r in regs:
        fields = r.get("fields") or {}
        for role in ROLES:
            p = ROLE_FIELD_PREFIX[role]
            phone = normalize_phone(fields.get(f"{p}手机号"))
            if not phone:
                continue
            is_owner = role == "主报名人"
            cur = phone_reg.get(phone)
            if cur is None or (is_owner and not cur[1]):
                phone_reg[phone] = (r["record_id"], is_owner)

    # ---------- 1. 队伍表状态字段 ----------
    team_updates: list[dict] = []
    for t in teams:
        f = t.get("fields") or {}
        rid = link_text(f.get("报名记录ID"))
        src = reg_by_rid.get(rid)
        patch = {}
        if not select_one(f.get("是否预组队")):
            # 进队伍表的行必然是预组队报名；报名表值缺失时按创建路径默认「是」
            patch["是否预组队"] = select_one(src.get("是否预组队")) if src else "是"
            if not patch["是否预组队"]:
                patch["是否预组队"] = "是"
        if not select_one(f.get("同意统一分配")):
            # 同步器创建路径默认：报名表无值视为「否」
            patch["同意统一分配"] = (select_one(src.get("是否同意统一分配")) if src else "") or "否"
        if patch:
            team_updates.append({"record_id": t["record_id"], "队伍ID": f.get("队伍ID", ""),
                                 "fields": patch, "来源": rid or "<无报名记录ID>"})

    print(f"\n== 队伍表待回填 {len(team_updates)} 行 ==")
    for u in team_updates:
        print(f"  {u['队伍ID'] or u['record_id']}  {u['fields']}  (报名 {u['来源']})")

    # ---------- 2. 选手表 报名记录ID ----------
    contestant_updates: list[dict] = []
    for c in contestants:
        f = c.get("fields") or {}
        if f.get("报名记录ID"):
            continue
        phone = normalize_phone(f.get("手机号"))
        hit = phone_reg.get(phone)
        if not hit:
            continue
        contestant_updates.append({
            "record_id": c["record_id"],
            "fields": {"报名记录ID": hit[0]},
            "选手ID": f.get("选手ID", ""), "姓名": f.get("姓名", ""),
        })

    print(f"\n== 选手表待回填 报名记录ID {len(contestant_updates)} 行 ==")
    for u in contestant_updates[:30]:
        print(f"  {u['选手ID']} {u['姓名']}  -> {u['fields']['报名记录ID']}")
    if len(contestant_updates) > 30:
        print(f"  ... 其余 {len(contestant_updates) - 30} 行省略")

    if not APPLY:
        print(f"\n[干跑] 共 队伍 {len(team_updates)} 行 + 选手 {len(contestant_updates)} 行。"
              f"加 --apply 实际写入。")
        return

    if team_updates:
        db_store.batch_update(CFG.tbl_teams,
                              [{"record_id": u["record_id"], "fields": u["fields"]} for u in team_updates])
        print(f"队伍表已写入 {len(team_updates)} 行")
    if contestant_updates:
        db_store.batch_update(CFG.tbl_contestants,
                              [{"record_id": u["record_id"], "fields": u["fields"]} for u in contestant_updates])
        print(f"选手表已写入 {len(contestant_updates)} 行")
    print("完成。机器人侧可执行「重载数据」刷新内存镜像。")


if __name__ == "__main__":
    main()
