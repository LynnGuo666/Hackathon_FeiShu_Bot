"""机器人入口。

启动内容：
- WebSocket 长连接接收消息事件与卡片回调（无需公网回调 URL）
- APScheduler 执行各模块注册的定时任务（如报名表自动同步）

用法：python -m bot.main
"""
from __future__ import annotations

import asyncio
import logging

from .core.card_kit import guide_card
from .core.event_bus import handle_card_action, handle_message
from .core.lark_client import send_card
from .core.registry import REGISTRY, MsgCtx

# 导入即注册：sync（同步指令+定时同步）、auth（验证）、group（补拉）、activity（活跃度）、vote（投票）
from .modules.sync import sync  # noqa: F401
from .modules.auth import auth  # noqa: F401
from .modules.group import group  # noqa: F401
from .modules.activity import activity  # noqa: F401
from .modules.vote import vote  # noqa: F401
from .modules.score import score  # noqa: F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bot")


@REGISTRY.on_fallback
async def fallback(ctx: MsgCtx) -> None:
    await send_card(ctx.open_id, guide_card())


def start_scheduler() -> "asyncio.AbstractEventLoop":
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    for name, interval, fn in REGISTRY.jobs:
        scheduler.add_job(fn, "interval", minutes=interval, id=name, name=name)
        log.info("注册定时任务 %s：每 %d 分钟", name, interval)
    scheduler.start()
    return scheduler


def main() -> None:
    import lark_oapi as lark

    from .core.config import CFG
    if not CFG.has_app_credentials:
        raise SystemExit("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请复制 .env.example 为 .env 并填写。")

    async def _run_jobs_forever():
        start_scheduler()
        while True:
            await asyncio.sleep(3600)

    dispatcher = lark.EventDispatcher.builder("").register_p2_im_message_receive_v1(
        lambda data: asyncio.get_event_loop().run_until_complete(handle_message(_to_dict(data)))
    ).build()

    # 卡片回调（需在开发者后台开启回调配置）；不同 SDK 版本方法名可能不同，失败则提示
    card_registered = True
    try:
        dispatcher = (lark.EventDispatcher.builder("")
                      .register_p2_im_message_receive_v1(
                          lambda data: asyncio.get_event_loop().run_until_complete(handle_message(_to_dict(data))))
                      .register_p2_card_action_trigger(
                          lambda data: asyncio.get_event_loop().run_until_complete(handle_card_action(_to_dict(data))))
                      .build())
    except AttributeError:
        card_registered = False
        log.warning("当前 SDK 版本不支持 register_p2_card_action_trigger，卡片回调不可用")

    from lark_oapi.ws import Client as WsClient
    ws = WsClient(CFG.app_id, CFG.app_secret, event_handler=dispatcher, log_level=lark.LogLevel.INFO)
    log.info("机器人启动，WebSocket 长连接运行中…（卡片回调%s）", "已启用" if card_registered else "未启用")
    import threading
    threading.Thread(target=lambda: asyncio.new_event_loop().run_until_complete(_run_jobs_forever()),
                     daemon=True).start()
    ws.start()


def _to_dict(data) -> dict:
    """lark-oapi P2 事件对象转 dict（header/event 两段）。"""
    header = getattr(data, "header", None)
    event = getattr(data, "event", None)
    out: dict = {}
    if event is not None:
        if hasattr(event, "__dict__"):
            out = json_default(event)
    if header is not None and out.get("header") is None:
        out["header"] = json_default(header)
        out["event"] = json_default(event)
    return out


def json_default(obj) -> dict:
    import dataclasses
    if dataclasses.is_dataclass(obj):
        return {k: _plain(v) for k, v in obj.__dict__.items()}
    if hasattr(obj, "__dict__"):
        return {k: _plain(v) for k, v in obj.__dict__.items()}
    return obj


def _plain(v):
    if hasattr(v, "__dict__"):
        return json_default(v)
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


if __name__ == "__main__":
    main()
