"""插件注册器：功能模块通过注册命令 / 卡片回调 / 定时任务接入，核心不改。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class MsgCtx:
    """一条私聊/群消息的上下文。"""
    open_id: str          # 发送者 open_id
    chat_id: str
    chat_type: str        # p2p / group
    text: str             # 纯文本内容（已去 @）
    message_id: str
    raw: dict             # 原始事件


@dataclass
class CardCtx:
    """一次卡片按钮/表单回调的上下文。"""
    open_id: str
    message_id: str
    chat_id: str
    action_value: str      # 按钮 action_value
    form_value: dict       # form 容器内全部控件值
    token: str             # 延迟更新卡片用
    raw: dict


CommandHandler = Callable[[MsgCtx], Awaitable[None]]
CardHandler = Callable[[CardCtx], Awaitable[None]]
JobHandler = Callable[[], Awaitable[None]]
GroupMsgHook = Callable[[str, str], Awaitable[None]]  # (open_id, chat_id)


@dataclass
class Registry:
    commands: dict[str, CommandHandler] = field(default_factory=dict)
    card_actions: dict[str, CardHandler] = field(default_factory=dict)
    jobs: list[tuple[str, int, JobHandler]] = field(default_factory=list)  # (name, interval_minutes, fn)
    group_msg_hooks: list[GroupMsgHook] = field(default_factory=list)
    fallback: CommandHandler | None = None

    def on_group_message(self):
        """注册群消息钩子（每条群消息都会回调，参数 open_id/chat_id），用于活跃度等统计。"""
        def deco(fn: GroupMsgHook):
            self.group_msg_hooks.append(fn)
            return fn
        return deco

    def command(self, *names: str):
        """注册文本指令：@bot.command("验证", "授权")"""
        def deco(fn: CommandHandler):
            for n in names:
                self.commands[n.strip()] = fn
            return fn
        return deco

    def on_card(self, action_value: str):
        """注册卡片回调（按按钮 action_value 路由）。"""
        def deco(fn: CardHandler):
            self.card_actions[action_value] = fn
            return fn
        return deco

    def job(self, name: str, interval_minutes: int):
        """注册定时任务。"""
        def deco(fn: JobHandler):
            self.jobs.append((name, interval_minutes, fn))
            return fn
        return deco

    def on_fallback(self, fn: CommandHandler) -> CommandHandler:
        self.fallback = fn
        return fn


REGISTRY = Registry()
