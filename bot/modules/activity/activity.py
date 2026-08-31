"""活跃度模块：监控群发言，按天聚合记录。

机制：
- 每条群消息（含非文本）触发 on_group_message 钩子，发言者是已验证选手时内存计数 +1
  （同时累加选手表「发言数」总计数，内存缓冲）；
- 定时任务（默认每 1 分钟）把缓冲统一落库：活跃度表一人一天一条记录（选手/日期/当天发言数），
  已有记录则累加，不写逐条流水；
- 「活跃」指令：查看当天/累计排行。
"""
from __future__ import annotations

import datetime as _dt
import logging

from ...core.base_store import BaseStore
from ...core.card_kit import result_card
from ...core.config import CFG
from ...core.lark_client import send_card
from ...core.registry import REGISTRY, MsgCtx

log = logging.getLogger(__name__)

# 内存缓冲：open_id -> {"record_id": str, "name": str, "delta": int}
_pending: dict[str, dict] = {}


def record_message(open_id: str) -> None:
    """记一次发言（已验证选手才记）。由 event_bus 在每条群消息上调用，只操作内存。"""
    if not open_id:
        return
    entry = _pending.get(open_id)
    if entry is None:
        rec = _find_by_open_id(open_id)
        if rec is None:
            return
        f = rec.get("fields") or {}
        entry = _pending[open_id] = {
            "record_id": rec["record_id"],
            "name": str(f.get("姓名") or f.get("选手ID") or "?"),
            "total": int(f.get("发言数") or 0),
            "delta": 0,
        }
    entry["delta"] += 1
    entry["total"] += 1


def _find_by_open_id(open_id: str) -> dict | None:
    store = BaseStore(CFG.db_base_token)
    for r in store.list_records(CFG.tbl_contestants):
        if str((r.get("fields") or {}).get("飞书open_id") or "") == open_id:
            return r
    return None


def flush() -> int:
    """把内存缓冲落库：活跃度表按天 upsert + 选手表发言数回写。返回落库人数。"""
    if not _pending:
        return 0
    today = _dt.date.today().isoformat()
    store = BaseStore(CFG.db_base_token)

    # 活跃度表现有今日记录：选手record_id -> {record_id, 发言数}
    existing: dict[str, dict] = {}
    for r in store.list_records(CFG.tbl_activity):
        f = r.get("fields") or {}
        if str(f.get("日期") or "") != today:
            continue
        for link in (f.get("选手") or []):
            if isinstance(link, dict) and link.get("record_id"):
                existing[link["record_id"]] = r

    a_create, a_update, c_update = [], [], []
    for open_id, e in _pending.items():
        if e["delta"] <= 0:
            continue
        rid = e["record_id"]
        old = existing.get(rid)
        if old:
            cur = int((old.get("fields") or {}).get("发言数") or 0)
            a_update.append({"record_id": old["record_id"], "fields": {"发言数": cur + e["delta"]}})
        else:
            a_create.append({"选手": [{"id": rid}], "日期": today, "发言数": e["delta"]})
        c_update.append({"record_id": rid, "fields": {"发言数": e["total"]}})
        e["delta"] = 0

    if a_create:
        store.batch_create(CFG.tbl_activity, a_create)
    if a_update:
        store.batch_update(CFG.tbl_activity, a_update)
    if c_update:
        store.batch_update(CFG.tbl_contestants, c_update)
    # 清掉已无增量的缓存项，避免长期驻留
    for k in [k for k, v in _pending.items() if v["delta"] <= 0]:
        _pending.pop(k, None)
    log.info("活跃度落库: 新建%d 更新%d", len(a_create), len(a_update))
    return len(a_create) + len(a_update)


def leaderboard(top_n: int = 10, daily: bool = False) -> list[tuple[str, int]]:
    """发言排行。daily=True 看当天（活跃度表），否则看累计（选手表发言数）。"""
    store = BaseStore(CFG.db_base_token)
    rows: list[tuple[str, int]] = []
    if daily:
        today = _dt.date.today().isoformat()
        name_by_rid = {r["record_id"]: str((r.get("fields") or {}).get("姓名") or (r.get("fields") or {}).get("选手ID") or "?")
                       for r in store.list_records(CFG.tbl_contestants)}
        for r in store.list_records(CFG.tbl_activity):
            f = r.get("fields") or {}
            if str(f.get("日期") or "") != today:
                continue
            for link in (f.get("选手") or []):
                if isinstance(link, dict) and link.get("record_id"):
                    rows.append((name_by_rid.get(link["record_id"], "?"), int(f.get("发言数") or 0)))
    else:
        for r in store.list_records(CFG.tbl_contestants):
            f = r.get("fields") or {}
            n = int(f.get("发言数") or 0)
            if n > 0:
                rows.append((str(f.get("姓名") or f.get("选手ID") or "?"), n))
    rows.sort(key=lambda x: -x[1])
    return rows[:top_n]


async def handle_activity(ctx: MsgCtx) -> None:
    from ..sync import is_admin
    daily = "今天" in ctx.text
    board = leaderboard(10, daily=daily)
    title = "今日发言排行" if daily else "发言活跃度排行（累计）"
    lines = [f"**{title} Top 10**", ""]
    if not board:
        lines.append("暂无发言数据。")
    else:
        for i, (name, n) in enumerate(board, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            lines.append(f"{medal} **{name}** —— {n} 条")
    if is_admin(ctx.open_id):
        total = sum(n for _, n in leaderboard(10**9))
        lines += ["", f"选手总发言（累计）：{total} 条"]
    await send_card(ctx.open_id, result_card("活跃度排行", True, lines))


REGISTRY.command("活跃", "活跃度")(handle_activity)


@REGISTRY.on_group_message()
async def count_group_message(open_id: str, chat_id: str) -> None:
    record_message(open_id)


@REGISTRY.job("活跃度落库", 1)
async def flush_job() -> None:
    flush()
