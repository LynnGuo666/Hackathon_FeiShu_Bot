"""活跃度模块：监控群发言，按天聚合记录。

机制：
- 每条群消息（含非文本）触发 on_group_message 钩子，发言者是已验证选手时内存计数 +1
  （同时累加选手表「发言数」总计数，内存缓冲）；
- 定时任务（默认每 1 分钟）把缓冲统一落库：活跃度表一人一天一条记录（选手/日期/当天发言数），
  已有记录则累加，不写逐条流水；
- 「活跃」指令：查看当天/累计排行。

并发（B3）：_pending 由事件循环线程写、落库任务读清，全程持锁；
落库成功后才清零 delta，写失败保留缓冲等下一轮重试。
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading

from ...core.card_kit import result_card
from ...core.lark_client import send_card
from ...core.registry import REGISTRY, MsgCtx
from ...core.service import SVC

log = logging.getLogger(__name__)

# 内存缓冲：open_id -> {"record_id": str, "name": str, "total": int, "delta": int}
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()


def record_message(open_id: str) -> None:
    """记一次发言（已验证选手才记）。由 event_bus 在每条群消息上调用。

    首次遇到某用户需要查选手表（可能全表扫描），放线程池执行避免阻塞事件循环（B1）。
    """
    if not open_id:
        return

    def _bump() -> None:
        with _pending_lock:
            entry = _pending.get(open_id)
        if entry is None:
            # 简化：直接同步查（本函数运行在事件循环线程，repo 的 to_thread 需要循环）
            # 因此这里改为：先占位，由异步钩子补全。见 count_group_message。
            return
        with _pending_lock:
            entry["delta"] += 1
            entry["total"] += 1


async def _lookup_contestant(open_id: str) -> dict | None:
    me = await SVC.contestants.get_by_open_id(open_id)
    if me is None:
        return None
    return {"record_id": me.record_id, "name": me.name or me.contestant_no or "?",
            "total": me.msg_count, "delta": 0}


async def count_group_message(open_id: str, chat_id: str) -> None:
    """群消息钩子（异步）：确保缓冲条目存在后计数 +1。"""
    if not open_id:
        return
    with _pending_lock:
        entry = _pending.get(open_id)
    if entry is None:
        info = await _lookup_contestant(open_id)
        if info is None:
            return
        with _pending_lock:
            entry = _pending.setdefault(open_id, info)
    with _pending_lock:
        entry["delta"] += 1
        entry["total"] += 1


async def flush() -> int:
    """把内存缓冲落库：活跃度表按天 upsert + 选手表发言数回写。返回落库人数。

    落库成功后才清零 delta；失败保留缓冲并抛出异常，等下一轮重试（B3）。
    """
    with _pending_lock:
        snapshot = {k: dict(v) for k, v in _pending.items() if v["delta"] > 0}
    if not snapshot:
        return 0
    today = _dt.date.today().isoformat()

    flushed = 0
    for open_id, e in snapshot.items():
        rid = e["record_id"]
        await SVC.activity.upsert_daily(rid, today, e["delta"])
        await SVC.activity.flush_totals([(rid, e["total"])])
        flushed += 1
        # 落库成功：扣减已落库的增量（保留落库期间新增的部分）
        with _pending_lock:
            cur = _pending.get(open_id)
            if cur is not None:
                cur["delta"] -= e["delta"]
                if cur["delta"] <= 0:
                    _pending.pop(open_id, None)
    log.info("活跃度落库: %d 人", flushed)
    return flushed


async def leaderboard(top_n: int = 10, daily: bool = False) -> list[tuple[str, int]]:
    """发言排行。daily=True 看当天（活跃度表），否则看累计（选手表发言数）。"""
    rows: list[tuple[str, int]] = []
    if daily:
        today = _dt.date.today().isoformat()
        name_by_rid = {c.record_id: c.name or c.contestant_no or "?"
                       for c in await SVC.contestants.list_all()}
        for e in await SVC.activity.today_rows(today):
            rows.append((name_by_rid.get(e.contestant_id, "?"), e.msg_count))
    else:
        for c in await SVC.contestants.list_all():
            if c.msg_count > 0:
                rows.append((c.name or c.contestant_no or "?", c.msg_count))
    rows.sort(key=lambda x: -x[1])
    return rows[:top_n]


async def handle_activity(ctx: MsgCtx) -> None:
    daily = "今天" in ctx.text
    board = await leaderboard(10, daily=daily)
    title = "今日发言排行" if daily else "发言活跃度排行（累计）"
    lines = [f"**{title} Top 10**", ""]
    if not board:
        lines.append("暂无发言数据。")
    else:
        for i, (name, n) in enumerate(board, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            lines.append(f"{medal} **{name}** —— {n} 条")
    if ctx.is_admin:
        total = sum(n for _, n in await leaderboard(10**9))
        lines += ["", f"选手总发言（累计）：{total} 条"]
    await send_card(ctx.open_id, result_card("活跃度排行", True, lines))


async def flush_job() -> None:
    try:
        await flush()
    except Exception:
        log.exception("活跃度落库失败（缓冲保留，下轮重试）")


def register() -> None:
    """注册用户侧活跃度指令、群消息钩子和落库任务。"""
    REGISTRY.user_command("活跃", "活跃度")(handle_activity)
    REGISTRY.on_group_message()(count_group_message)
    REGISTRY.job("活跃度落库", 1)(flush_job)
