"""管理员指令处理器。

这里仅编排领域服务和管理员反馈，不把管理员权限判断散落到各业务模块；
是否有权限由 ``core.event_bus`` 按注册作用域统一处理。

存储一律经 SVC 仓库接口（core.service），不直接接触飞书字段。
"""
from __future__ import annotations

import asyncio

from ...core.card_kit import result_card
from ...core.lark_client import create_group, send_card, send_text
from ...core.models import GroupConfig
from ...core.permissions import admins_status_line
from ...core.registry import REGISTRY, MsgCtx
from ...core.service import SVC
from ..auth.binding_codes import generate_binding_codes
from ..group.group import pull_user_into_groups, verified_users
from ..sync.sync import run_sync, sync_in_progress
from ..vote.state import set_vote_open


async def handle_sync(ctx: MsgCtx) -> None:
    if sync_in_progress():
        await send_text(ctx.open_id, "已有同步正在进行，请稍候再试。")
        return
    await send_text(ctx.open_id, "开始同步报名表，请稍候…")
    stats = await run_sync()
    await send_card(ctx.open_id, result_card(
        "同步完成", True,
        [f"- {key}：**{value}**" for key, value in stats.items()]))


async def handle_admins(ctx: MsgCtx) -> None:
    await send_card(ctx.open_id, result_card("管理员", True, [admins_status_line()]))


async def handle_backfill(ctx: MsgCtx) -> None:
    targets = await verified_users()
    if not targets:
        await send_text(ctx.open_id, "没有已验证的用户。")
        return
    ok, fail = await pull_user_into_groups(targets)
    await send_card(ctx.open_id, result_card("补拉完成", True, [
        f"- 已验证用户：{len(targets)}",
        f"- 本次补拉入群人次：{ok}",
        *([f"- 失败：{fail}"] if fail else []),
    ]))


async def handle_create_group(ctx: MsgCtx) -> None:
    """创建外部群并登记群配置。"""
    parts = ctx.text.split(maxsplit=2)
    if len(parts) < 2 or not parts[1].strip():
        await send_text(
            ctx.open_id,
            "用法：建群 <群名> [面向身份]\n"
            "面向身份可选：选手 / 组委会-导师 / 组委会-主办方 / 全部（默认选手，可逗号分隔多个）",
        )
        return

    name = parts[1].strip()
    audiences = [
        audience.strip()
        for audience in (parts[2].split(",") if len(parts) > 2 else [])
        if audience.strip()
    ] or ["选手"]

    try:
        chat_id = await asyncio.to_thread(
            create_group,
            name,
            owner_open_id=ctx.open_id,
            member_open_ids=[ctx.open_id],
            description=f"{name}（机器人创建）",
            external=True,
        )
    except Exception as exc:
        await send_card(ctx.open_id, result_card("建群失败", False, [str(exc)]))
        return

    await SVC.groups.create_config(GroupConfig(
        name=name, chat_id=chat_id, enabled=True, audiences=audiences,
        note=f"机器人创建，群主 open_id: {ctx.open_id}"))
    await send_card(ctx.open_id, result_card("建群成功", True, [
        f"**{name}** 已创建并拉你入群（你是群主）。",
        "",
        f"- chat_id：`{chat_id}`",
        f"- 面向身份：{'、'.join(audiences)}",
        "- 已登记进群配置表，验证/补拉会自动按身份拉人。",
        "- 外部群可拉外部成员；如需拉人请把对方 open_id 提供给机器人或手动拉入。",
    ]))


async def handle_generate_codes(ctx: MsgCtx) -> None:
    generated = await generate_binding_codes()
    if not generated:
        await send_card(ctx.open_id, result_card(
            "生成绑定码", True, ["所有组委会成员都已有绑定码，无需生成。"]))
        return

    listing = [
        f"- **{item['name']}**（{item['identity']}）：`{item['code']}`"
        for item in generated
    ]
    await send_card(ctx.open_id, result_card("绑定码已生成", True, [
        f"共生成 {len(generated)} 个，请分发给对应成员：",
        "",
        *listing,
    ]))


async def handle_vote_toggle(ctx: MsgCtx) -> None:
    opened = ctx.text == "开票"
    set_vote_open(opened)
    state = "开放" if opened else "关闭"
    await send_text(ctx.open_id, f"投票通道已{state}。")


def register() -> None:
    """注册管理员侧指令。"""
    REGISTRY.admin_command("同步")(handle_sync)
    REGISTRY.admin_command("管理员")(handle_admins)
    REGISTRY.admin_command("补拉")(handle_backfill)
    REGISTRY.admin_command("建群")(handle_create_group)
    REGISTRY.admin_command("生成绑定码")(handle_generate_codes)
    REGISTRY.admin_command("开票", "关票")(handle_vote_toggle)


__all__ = ["register"]
