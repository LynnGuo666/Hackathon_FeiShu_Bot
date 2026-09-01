"""积分模块：积分表写流水 + 选手表「积分」总分同步更新。

- add_score(contestant_record_id, delta, reason)：写一条流水并把选手表总分 ±delta；
- 「查分」指令：查看自己的积分与最近流水；管理员可「加分 @不适用」暂不支持，直接改表即可。

存储一律经 SVC 仓库接口（core.service），不直接接触飞书字段。
"""
from __future__ import annotations

import asyncio
import logging

from ...core.card_kit import result_card
from ...core.lark_client import send_card
from ...core.registry import REGISTRY, MsgCtx
from ...core.service import SVC

log = logging.getLogger(__name__)

# 同一选手的积分读改写串行化（B3）：防止并发加分互相覆盖总分
_score_locks: dict[str, asyncio.Lock] = {}
_score_locks_guard = asyncio.Lock()


async def _score_lock(contestant_record_id: str) -> asyncio.Lock:
    async with _score_locks_guard:
        return _score_locks.setdefault(contestant_record_id, asyncio.Lock())


async def add_score(contestant_record_id: str, delta: int, reason: str) -> int:
    """写积分流水并更新选手表总分，返回变动后总分。

    读改写用选手粒度锁串行化（B3），防止并发加分互相覆盖总分。
    """
    contestants = SVC.contestants
    lock = await _score_lock(contestant_record_id)
    async with lock:
        me = await contestants.get(contestant_record_id)
        total = me.score if me else 0
        new_total = total + delta
        await SVC.scores.add(contestant_record_id, delta, reason, new_total)
        await contestants.update(contestant_record_id, score=new_total)
    return new_total


async def handle_my_score(ctx: MsgCtx) -> None:
    me = await SVC.contestants.get_by_open_id(ctx.open_id)
    if me is None:
        await send_card(ctx.open_id, result_card("我的积分", False, ["你还未完成验证，先发「验证」。"]))
        return
    lines = [f"**{me.name}**（{me.contestant_no}）当前积分：**{me.score}**", "", "**最近流水**"]
    recent = await SVC.scores.history(me.record_id, limit=5)
    if not recent:
        lines.append("- 暂无积分记录")
    else:
        for entry in recent:
            lines.append(f"- {'+' if entry.delta >= 0 else ''}{entry.delta}　{entry.reason}")
    await send_card(ctx.open_id, result_card("我的积分", True, lines))


def register() -> None:
    """注册用户侧积分查询指令。"""
    REGISTRY.user_command("查分", "积分")(handle_my_score)
