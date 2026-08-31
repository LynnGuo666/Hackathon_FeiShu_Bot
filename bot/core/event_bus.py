"""事件分发：把飞书事件路由到 registry 中注册的处理器。"""
from __future__ import annotations

import json
import logging

from .registry import REGISTRY, CardCtx, MsgCtx

log = logging.getLogger(__name__)


def _text_of(content: str) -> str:
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return ""
    text = data.get("text", "")
    # 群里 @机器人 的文本形如 "@助手 验证"，去掉 @ 片段
    return text.replace("@_user_1", "").strip()


async def handle_message(event: dict) -> None:
    msg = event.get("message", {})
    sender = (event.get("sender") or {}).get("sender_id", {})
    open_id = sender.get("open_id", "")
    chat_type = msg.get("chat_type", "")
    if msg.get("message_type") != "text":
        return
    ctx = MsgCtx(open_id=open_id, chat_id=msg.get("chat_id", ""), chat_type=chat_type,
                 text=_text_of(msg.get("content", "")), message_id=msg.get("message_id", ""), raw=event)
    handler = REGISTRY.commands.get(ctx.text)
    if handler is None:
        handler = REGISTRY.fallback
    if handler is not None:
        try:
            await handler(ctx)
        except Exception:
            log.exception("处理消息指令出错: %s", ctx.text)
    # 群消息钩子（活跃度统计等），指令消息也计入发言
    if ctx.chat_type == "group":
        for hook in REGISTRY.group_msg_hooks:
            try:
                await hook(ctx.open_id, ctx.chat_id)
            except Exception:
                log.exception("群消息钩子出错")


async def handle_card_action(event: dict) -> None:
    action = (event.get("action") or {})
    ctx = CardCtx(
        open_id=event.get("operator_id", {}).get("open_id", ""),
        message_id=event.get("context", {}).get("open_message_id", ""),
        chat_id=(event.get("context") or {}).get("open_chat_id", ""),
        action_value=str(action.get("value", {}).get("action", "")) if isinstance(action.get("value"), dict) else "",
        form_value=action.get("form_value") or {},
        token=event.get("token", ""),
        raw=event,
    )
    handler = REGISTRY.card_actions.get(ctx.action_value)
    if handler is None:
        return
    try:
        await handler(ctx)
    except Exception:
        log.exception("处理卡片回调出错: %s", ctx.action_value)
