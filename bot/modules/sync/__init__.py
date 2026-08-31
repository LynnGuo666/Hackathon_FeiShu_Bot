"""同步模块：注册「同步」指令与定时同步任务。"""
from __future__ import annotations

from ...core.card_kit import result_card
from ...core.config import CFG
from ...core.lark_client import send_card, send_text
from ...core.registry import REGISTRY, MsgCtx
from .sync import run_sync


def is_admin(open_id: str) -> bool:
    return open_id in CFG.admin_open_ids


async def handle_sync(ctx: MsgCtx) -> None:
    await send_text(ctx.open_id, "开始同步报名表，请稍候…")
    stats = run_sync()
    await send_card(ctx.open_id, result_card(
        "同步完成", True,
        [f"- {k}：**{v}**" for k, v in stats.items()]))


REGISTRY.command("同步")(handle_sync)


@REGISTRY.job("报名表自动同步", CFG.sync_interval_minutes)
async def sync_job() -> None:
    run_sync()
