"""积分插件：积分表写流水，总分由 Base 公式字段（SUM 流水）服务端聚合。

- add_score(contestant_record_id, delta, reason)：写一条流水；镜像内总分同步累加
  供即时展示，不再读改写回选手表（消灭并发覆盖问题，无需选手粒度锁）。
- 「查分」指令：查看自己的积分与最近流水。
"""
from __future__ import annotations

import logging

from ...core.card_kit import result_card
from ...core.lark_client import send_card
from ...core.plugin import Plugin
from ...core.registry import REGISTRY, MsgCtx
from ...core.service import SVC

log = logging.getLogger(__name__)


async def add_score(contestant_record_id: str, delta: int, reason: str) -> int:
    """写积分流水并返回变动后总分（镜像即时值；权威值由公式字段聚合）。

    写路径单条流水 1 次 API；总分 = 流水 SUM，无读改写竞态，无需加锁。
    """
    me = await SVC.contestants.get(contestant_record_id)
    total = me.score if me else 0
    new_total = total + delta
    await SVC.scores.add(contestant_record_id, delta, reason, new_total)
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


class ScorePlugin(Plugin):
    name = "score"
    dependencies = ()

    def setup(self) -> None:
        REGISTRY.user_command("查分", "积分", plugin=self.name)(handle_my_score)
