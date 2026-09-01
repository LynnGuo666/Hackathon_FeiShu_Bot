"""投票插件：决赛投票环节。

- 选手私聊发「投票」→ 机器人回项目列表卡片（每个项目一个投票按钮）
- 点按钮 → card.action.trigger 回调 → 校验（已验证选手、未投过票）→ 写投票表
- 「票数」指令：查看当前票榜；「开票/关票」：管理员开关投票

票数权威值 = 投票表记录数（Base 公式/查找引用字段服务端聚合）；
镜像内项目票数同步 +1 仅供即时展示。写路径每次投票恰好 1 次 API 调用。
"""
from __future__ import annotations

import asyncio
import logging

from ...core.card_kit import result_card
from ...core.lark_client import send_card, send_text
from ...core.models import Contestant, Project
from ...core.plugin import Plugin
from ...core.registry import REGISTRY, CardCtx, MsgCtx
from ...core.service import SVC
from .state import is_vote_open

log = logging.getLogger(__name__)

# 同一用户的投票流程串行化（B3）：防止去重窗口外的并发双投
_voter_locks: dict[str, asyncio.Lock] = {}
_voter_locks_guard = asyncio.Lock()


async def _voter_lock(open_id: str) -> asyncio.Lock:
    async with _voter_locks_guard:
        return _voter_locks.setdefault(open_id, asyncio.Lock())


async def list_projects() -> list[Project]:
    projects = await SVC.projects.list_all()
    projects.sort(key=lambda p: p.project_no)
    return projects


async def handle_vote(ctx: MsgCtx) -> None:
    if not is_vote_open():
        await send_card_result(ctx.open_id, "投票未开放", False, ["投票通道当前已关闭，请等待主持人开票。"])
        return
    me = await SVC.contestants.get_by_open_id(ctx.open_id)
    if me is None:
        await send_card_result(ctx.open_id, "无法投票", False, ["你还未完成验证，请先发送「验证」。"])
        return
    await send_card(ctx.open_id, await project_list_card())


async def project_list_card() -> dict:
    """项目列表卡片（JSON 2.0）：每个项目一个投票按钮（behaviors callback）。票数不公开。"""
    projects = await list_projects()
    elements = []
    if not projects:
        elements.append({"tag": "markdown", "content": "项目表暂无项目。"})
    for p in projects:
        elements.append({"tag": "markdown",
                         "content": f"**{p.project_no} {p.name}**"})
        elements.append({"tag": "button",
                         "text": {"tag": "plain_text", "content": f"投给 {p.name}"},
                         "type": "primary", "size": "medium",
                         "behaviors": [{"type": "callback",
                                        "value": {"action": "vote",
                                                  "project_record_id": p.record_id}}]})
    return {"schema": "2.0",
            "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "决赛投票"}, "template": "violet"},
            "body": {"elements": elements}}


async def handle_vote_action(card_ctx: CardCtx) -> None:
    if not is_vote_open():
        await send_text(card_ctx.open_id, "投票通道已关闭。")
        return
    me = await SVC.contestants.get_by_open_id(card_ctx.open_id)
    if me is None:
        await send_text(card_ctx.open_id, "你还未完成验证，无法投票。")
        return
    project_rid = card_ctx.raw.get("action", {}).get("value", {}).get("project_record_id", "")
    if not project_rid:
        return
    # 同一用户串行：查已投 -> 写投票（B3）。写穿透 1 次 API，票数由公式字段聚合。
    lock = await _voter_lock(card_ctx.open_id)
    async with lock:
        voted = await SVC.votes.voted_project_ids(me.record_id)
        if project_rid in voted:
            await send_text(card_ctx.open_id, "你已经投过票了，一人一票哦。")
            return
        await SVC.votes.add(me.record_id, project_rid)
    project = await SVC.projects.get(project_rid)
    name = project.name if project else ""
    await send_text(card_ctx.open_id, f"投票成功 ✅ 你把票投给了「{name}」。")


async def handle_votes_board(ctx: MsgCtx) -> None:
    """票榜：票数不公开，只展示排名不带数字（管理员可见票数）。"""
    projects = sorted(await list_projects(), key=lambda p: -p.votes)
    show_votes = ctx.is_admin
    lines = ["**当前票榜**", ""]
    for i, p in enumerate(projects, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} **{p.name}**" + (f" —— {p.votes} 票" if show_votes else ""))
    if not show_votes:
        lines += ["", "票数暂不公开，最终结果以主办方公布为准。"]
    await send_card(ctx.open_id, result_card("票榜", True, lines))


class VotePlugin(Plugin):
    name = "vote"
    dependencies = ()

    def setup(self) -> None:
        REGISTRY.user_command("投票", plugin=self.name)(handle_vote)
        REGISTRY.user_command("票数", "票榜", plugin=self.name)(handle_votes_board)
        REGISTRY.on_card("vote", scope="user", plugin=self.name)(handle_vote_action)


async def send_card_result(open_id: str, title: str, ok: bool, lines: list[str]) -> None:
    await send_card(open_id, result_card(title, ok, lines))
