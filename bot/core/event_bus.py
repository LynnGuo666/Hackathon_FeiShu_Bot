"""事件分发：把飞书事件路由到 registry 中注册的处理器。"""
from __future__ import annotations

import json
import logging
import time

from .config import CFG
from .registry import REGISTRY, CardCtx, MsgCtx

log = logging.getLogger(__name__)

# 飞书会对超时/失败的事件重推，用 event_id 去重（保留 10 分钟窗口）
_seen_events: dict[str, float] = {}
_DEDUP_WINDOW = 600
# 卡片回调幂等：SDK ws 对 CARD 帧不回 ACK，飞书会重推；按「操作者+卡片+动作」去重
_seen_card_actions: dict[str, float] = _seen_events  # 复用同一个清理窗口字典会互相干扰，单独建
_seen_card_actions = {}
_CARD_DEDUP_WINDOW = 120  # 卡片操作 2 分钟内视为重复（正常点击不会这么密）


def _dedup(event: dict) -> bool:
    """返回 True 表示重复事件（应跳过）。"""
    eid = str((event.get("header") or {}).get("event_id") or "")
    if not eid:
        return False
    now = time.time()
    # 顺手清理过期项
    for k in [k for k, ts in _seen_events.items() if now - ts > _DEDUP_WINDOW]:
        _seen_events.pop(k, None)
    if eid in _seen_events:
        log.info("重复事件已跳过: %s", eid)
        return True
    _seen_events[eid] = now
    return False


def _card_dedup(ctx) -> bool:
    """卡片回调幂等：同一操作者对同一张卡片的同一动作，2 分钟内只处理一次。

    背景：lark-oapi ws 对卡片帧不回 ACK，飞书会重推卡片回调，导致一次点击多次执行。
    """
    now = time.time()
    for k in [k for k, ts in _seen_card_actions.items() if now - ts > _CARD_DEDUP_WINDOW]:
        _seen_card_actions.pop(k, None)
    key = f"{ctx.open_id}:{ctx.message_id}:{ctx.action_value}"
    if key in _seen_card_actions:
        log.info("重复卡片回调已跳过: %s", key)
        return True
    _seen_card_actions[key] = now
    return False


def _text_of(content: str) -> str:
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return ""
    text = data.get("text", "")
    # 群里 @机器人 的文本形如 "@助手 验证"，去掉 @ 片段
    return text.replace("@_user_1", "").strip()


def _is_external(event: dict) -> bool:
    """判断发送者是否企业外用户。

    im.message.receive_v1 里 sender_type 只有 user/app，外部用户靠 tenant_key 与本租户不同来识别。
    本租户 tenant_key 启动时从 tenant API 获取；拿不到时不拦截（宽松策略）。
    """
    sender = (event.get("sender") or {})
    own = CFG.own_tenant_key
    if not own:
        return False
    key = str(sender.get("tenant_key", ""))
    return bool(key) and key != own


async def handle_message(event: dict) -> None:
    if _dedup(event):
        return
    msg = event.get("message", {})
    sender = (event.get("sender") or {}).get("sender_id", {})
    open_id = sender.get("open_id", "")
    chat_type = msg.get("chat_type", "")
    if msg.get("message_type") != "text":
        return
    ctx = MsgCtx(open_id=open_id, chat_id=msg.get("chat_id", ""), chat_type=chat_type,
                 text=_text_of(msg.get("content", "")), message_id=msg.get("message_id", ""), raw=event)

    # 记录发送者身份（含外部用户 open_id，便于排查/拉群）
    if _is_external(event):
        log.info("收到外部用户消息: open_id=%s chat_type=%s text=%r", open_id, chat_type, ctx.text[:20])

    # 企业外用户：默认只回帮助卡片，不执行任何指令（外部用户读不到手机号，验证无意义）
    if _is_external(event) and not CFG.allow_external_users:
        if ctx.text in ("帮助", "help", "菜单"):
            handler = REGISTRY.fallback
            if handler:
                await handler(ctx)
        return

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
    if _dedup(event):
        return
    action = (event.get("action") or {})
    # 卡片回调的操作者字段是 operator（消息事件里才是 sender_id）
    operator = event.get("operator") or event.get("operator_id") or {}
    ctx = CardCtx(
        open_id=operator.get("open_id", ""),
        message_id=(event.get("context") or {}).get("open_message_id", ""),
        chat_id=(event.get("context") or {}).get("open_chat_id", ""),
        action_value=str(action.get("value", {}).get("action", "")) if isinstance(action.get("value"), dict) else "",
        form_value=action.get("form_value") or {},
        token=event.get("token", ""),
        raw=event,
    )
    if not ctx.open_id:
        log.warning("卡片回调缺少 operator.open_id，跳过: %s", list(event.keys()))
        return
    if _card_dedup(ctx):
        return
    # 企业外用户不响应卡片操作（投票等）
    if _is_external(event) and not CFG.allow_external_users:
        return
    handler = REGISTRY.card_actions.get(ctx.action_value)
    if handler is not None:
        try:
            await handler(ctx)
        except Exception:
            log.exception("处理卡片回调出错: %s", ctx.action_value)
        return
    # 帮助卡片快捷按钮：把按钮 action 映射为对应文本指令复用现有处理器
    _GUIDE_BUTTON_COMMANDS = {
        "verify": "验证", "vote": "投票", "activity": "活跃", "votes_board": "票数",
        "profile": "个人中心",
    }
    cmd = _GUIDE_BUTTON_COMMANDS.get(ctx.action_value)
    if cmd:
        handler = REGISTRY.commands.get(cmd)
        if handler is not None:
            try:
                # 按钮上下文转消息上下文（chat_type 用回调所在会话类型）
                msg_ctx = MsgCtx(open_id=ctx.open_id, chat_id=ctx.chat_id,
                                 chat_type="p2p" if not ctx.chat_id else "p2p",
                                 text=cmd, message_id=ctx.message_id, raw=ctx.raw)
                await handler(msg_ctx)
            except Exception:
                log.exception("处理卡片快捷按钮出错: %s", ctx.action_value)
