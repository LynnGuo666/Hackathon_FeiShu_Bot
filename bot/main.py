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
from .core.permissions import refresh_admins
from .core.registry import REGISTRY, MsgCtx

# 管理员和用户侧分别装配，领域模块只提供服务或注册自身能力。
from .modules.admin import register as register_admin_modules
from .modules.user import register as register_user_modules

register_admin_modules()
register_user_modules()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bot")


@REGISTRY.on_fallback
async def fallback(ctx: MsgCtx) -> None:
    await send_card(ctx.open_id, guide_card(is_admin_user=ctx.is_admin))


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
    """在事件回调线程里调度协程，立即返回 ACK（不阻塞等待）。

    SDK 回调若超时未返回，飞书会认为投递失败并重推事件，导致重复处理；
    因此这里只提交任务到后台循环，不做 .result() 同步等待。
    """
    try:
        loop = _BACKGROUND_LOOP
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)
            return
    except RuntimeError:
        pass
    asyncio.run(coro)


_BACKGROUND_LOOP: asyncio.AbstractEventLoop | None = None


def _start_background_loop():
    """独立事件循环线程：所有事件处理协程跑在这里，互不阻塞 ws 心跳。"""
    global _BACKGROUND_LOOP
    loop = asyncio.new_event_loop()
    _BACKGROUND_LOOP = loop
    threading.Thread(target=loop.run_forever, daemon=True, name="bot-event-loop").start()


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
    """定时任务统一跑在 bot 后台事件循环线程（B4）。

    AsyncIOScheduler 绑定该循环：job 协程与事件处理同线程，共享状态无需跨线程加锁；
    阻塞 IO 由仓库层 to_thread 解决。
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler(event_loop=_BACKGROUND_LOOP, timezone="Asia/Shanghai")
    for name, interval, fn in REGISTRY.jobs:
        scheduler.add_job(fn, "interval", minutes=interval, id=name, name=name)
        log.info("注册定时任务 %s：每 %d 分钟", name, interval)
    scheduler.start()


def fetch_tenant_key() -> None:
    """启动时获取本租户 tenant_key，用于识别外部用户（失败不阻塞启动）。"""
    import urllib.request

    from .core.config import CFG
    if CFG.own_tenant_key or not CFG.has_app_credentials:
        return
    try:
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": CFG.app_id, "app_secret": CFG.app_secret}).encode(),
            headers={"Content-Type": "application/json"})
        tok = json.load(urllib.request.urlopen(req, timeout=10))["tenant_access_token"]
        api = urllib.request.Request("https://open.feishu.cn/open-apis/tenant/v2/tenant/query",
                                     headers={"Authorization": f"Bearer {tok}"})
        d = json.load(urllib.request.urlopen(api, timeout=10))
        CFG.own_tenant_key = str((d.get("data") or {}).get("tenant", {}).get("tenant_key") or "")
        log.info("本租户 tenant_key: %s", CFG.own_tenant_key)
    except Exception as e:
        log.warning("获取 tenant_key 失败（外部用户拦截将不生效）: %s", e)


def main() -> None:
    import lark_oapi as lark
    from lark_oapi.ws import Client as WsClient

    from .core.config import CFG
    from .core.service import init_services
    if not CFG.has_app_credentials:
        raise SystemExit("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请复制 .env.example 为 .env 并填写。")

    init_services()  # 按 DATA_BACKEND 装配仓库（feishu / api 预留）
    fetch_tenant_key()
    refresh_admins()
    _start_background_loop()
    start_scheduler()

    ws = WsClient(CFG.app_id, CFG.app_secret,
                  event_handler=build_dispatcher(),
                  log_level=lark.LogLevel.INFO)
    log.info("机器人启动：WebSocket 长连接运行中（消息事件 + 卡片回调）…")
    ws.start()


if __name__ == "__main__":
    main()
