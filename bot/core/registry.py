"""插件注册器：功能模块通过显式注册接入指令、回调和定时任务。

支持按插件名注销（热插拔基础）：unregister_plugin 移除该插件注册的全部
commands / card_actions / group_msg_hooks / jobs。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal


CommandScope = Literal["public", "user", "admin"]
ChatScope = Literal["p2p", "group", "any"]


@dataclass
class MsgCtx:
    """一条私聊/群消息的上下文。"""
    open_id: str          # 发送者 open_id
    chat_id: str
    chat_type: str        # p2p / group
    text: str             # 纯文本内容（已去 @）
    message_id: str
    raw: dict             # 原始事件
    is_admin: bool = False  # 由事件路由层解析出的操作者权限


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
    is_admin: bool = False  # 由事件路由层解析出的操作者权限


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
    command_scopes: dict[str, CommandScope] = field(default_factory=dict)
    card_scopes: dict[str, CommandScope] = field(default_factory=dict)
    command_chats: dict[str, ChatScope] = field(default_factory=dict)
    # 插件归属：command/card 名 -> 插件名；job 名 -> 插件名（hooks 按插件分组存）
    owners: dict[str, str] = field(default_factory=dict)
    hook_owners: list[tuple[GroupMsgHook, str]] = field(default_factory=list)

    def on_group_message(self, plugin: str = ""):
        """注册群消息钩子（每条群消息都会回调，参数 open_id/chat_id），用于活跃度等统计。"""
        def deco(fn: GroupMsgHook):
            self.group_msg_hooks.append(fn)
            self.hook_owners.append((fn, plugin))
            return fn
        return deco

    def command(self, *names: str, scope: CommandScope = "public", chat: ChatScope = "p2p",
                plugin: str = ""):
        """注册文本指令，并声明权限作用域与适用会话（默认仅私聊触发）。"""
        if scope not in ("public", "user", "admin"):
            raise ValueError(f"未知指令作用域: {scope}")
        if chat not in ("p2p", "group", "any"):
            raise ValueError(f"未知会话范围: {chat}")

        def deco(fn: CommandHandler):
            for n in names:
                name = n.strip()
                self.commands[name] = fn
                self.command_scopes[name] = scope
                self.command_chats[name] = chat
                if plugin:
                    self.owners[name] = plugin
            return fn
        return deco

    def user_command(self, *names: str, chat: ChatScope = "p2p", plugin: str = ""):
        """注册用户侧指令。管理员也可以使用用户侧指令。"""
        return self.command(*names, scope="user", chat=chat, plugin=plugin)

    def admin_command(self, *names: str, chat: ChatScope = "p2p", plugin: str = ""):
        """注册管理员侧指令。事件路由层会统一执行权限校验。"""
        return self.command(*names, scope="admin", chat=chat, plugin=plugin)

    def public_command(self, *names: str, chat: ChatScope = "p2p", plugin: str = ""):
        """注册无需身份权限的公共指令。"""
        return self.command(*names, scope="public", chat=chat, plugin=plugin)

    def allows_command(self, name: str, is_admin: bool) -> bool:
        """判断操作者是否可以执行指令。未声明的旧指令按公共处理。"""
        return self.command_scopes.get(name, "public") != "admin" or is_admin

    def allows_chat(self, name: str, chat_type: str) -> bool:
        """判断指令是否允许在该会话类型中触发。未声明的旧指令按仅私聊处理。"""
        want = self.command_chats.get(name, "p2p")
        return want == "any" or want == chat_type

    def on_card(self, action_value: str, scope: CommandScope = "public", plugin: str = ""):
        """注册卡片回调，并声明它属于公共、用户或管理员侧。"""
        if scope not in ("public", "user", "admin"):
            raise ValueError(f"未知卡片作用域: {scope}")

        def deco(fn: CardHandler):
            self.card_actions[action_value] = fn
            self.card_scopes[action_value] = scope
            if plugin:
                self.owners[action_value] = plugin
            return fn
        return deco

    def allows_card(self, action_value: str, is_admin: bool) -> bool:
        """判断操作者是否可以执行卡片回调。"""
        return self.card_scopes.get(action_value, "public") != "admin" or is_admin

    def job(self, name: str, interval_minutes: int, plugin: str = ""):
        """注册定时任务。"""
        def deco(fn: JobHandler):
            self.jobs.append((name, interval_minutes, fn))
            if plugin:
                self.owners[name] = plugin
            return fn
        return deco

    def on_fallback(self, fn: CommandHandler) -> CommandHandler:
        self.fallback = fn
        return fn

    def unregister_plugin(self, plugin: str) -> dict[str, int]:
        """注销插件注册的全部能力，返回 {commands, cards, hooks, jobs} 移除计数。"""
        cmds = [n for n, owner in self.owners.items() if owner == plugin and n in self.commands]
        for n in cmds:
            self.commands.pop(n, None)
            self.command_scopes.pop(n, None)
            self.command_chats.pop(n, None)
        cards = [n for n, owner in self.owners.items() if owner == plugin and n in self.card_actions]
        for n in cards:
            self.card_actions.pop(n, None)
            self.card_scopes.pop(n, None)
        hooks = [fn for fn, owner in self.hook_owners if owner == plugin]
        for fn in hooks:
            if fn in self.group_msg_hooks:
                self.group_msg_hooks.remove(fn)
        jobs = [j for j in self.jobs if self.owners.get(j[0]) == plugin]
        for j in jobs:
            self.jobs.remove(j)
        # 清理 owners 中已不存在的条目
        for n in cmds + cards + [j[0] for j in jobs]:
            self.owners.pop(n, None)
        self.hook_owners = [(fn, owner) for fn, owner in self.hook_owners if owner != plugin]
        return {"commands": len(cmds), "cards": len(cards),
                "hooks": len(hooks), "jobs": len(jobs)}


REGISTRY = Registry()
