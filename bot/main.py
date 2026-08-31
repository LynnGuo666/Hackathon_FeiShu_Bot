"""机器人入口。

启动内容：
- WebSocket 长连接接收消息事件与卡片回调（无需公网回调 URL）
- APScheduler 执行各模块注册的定时任务（如报名表自动同步、活跃度落库）

用法：python -m bot.main
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

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


def _to_dict(obj) -> dict:
    """lark-oapi P2 事件对象递归转 dict。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_dict(x) for x in obj]
    if hasattr(obj, "__dict__"):
        return {k: _to_dict(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return obj


def _run_async(coro):
    """在事件回调线程里跑协程（SDK 回调是同步入口）。"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=25)
            return
    except RuntimeError:
        pass
    asyncio.run(coro)


def build_dispatcher():
    import lark_oapi as lark

    def on_message(data) -> None:
        event = _to_dict(data)
        _run_async(handle_message(event.get("event") or {}))

    def on_card(data) -> None:
        event = _to_dict(data)
        _run_async(handle_card_action(event.get("event") or {}))

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .register_p2_card_action_trigger(on_card)
               .build())
    return handler


def start_scheduler() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    for name, interval, fn in REGISTRY.jobs:
        # 定时任务里可能跑同步阻塞 IO，用独立线程池
        scheduler.add_job(_job_wrapper(fn), "interval", minutes=interval, id=name, name=name)
        log.info("注册定时任务 %s：每 %d 分钟", name, interval)
    scheduler.start()


def _job_wrapper(fn):
    def run():
        asyncio.run(fn())
    return run


def main() -> None:
    import lark_oapi as lark
    from lark_oapi.ws import Client as WsClient

    from .core.config import CFG
    if not CFG.has_app_credentials:
        raise SystemExit("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请复制 .env.example 为 .env 并填写。")

    start_scheduler()

    ws = WsClient(CFG.app_id, CFG.app_secret,
                  event_handler=build_dispatcher(),
                  log_level=lark.LogLevel.INFO)
    log.info("机器人启动：WebSocket 长连接运行中（消息事件 + 卡片回调）…")
    ws.start()


if __name__ == "__main__":
    main()
