"""个人中心模块：展示选手唯一 ID、队友信息、项目提交状态。

- 「个人中心」/「我的」指令或帮助卡按钮触发；
- 未验证用户提示先验证；
- 已验证用户展示：选手 ID、姓名、审核/验证状态、队伍（队长/队友）、项目提交状态。

存储一律经 SVC 仓库接口（core.service），不直接接触飞书字段。
"""
from __future__ import annotations

import logging

from ...core.card_kit import result_card
from ...core.lark_client import send_card
from ...core.models import Contestant, Organizer
from ...core.plugin import Plugin
from ...core.registry import REGISTRY, MsgCtx
from ...core.service import SVC

log = logging.getLogger(__name__)


async def _team_lines(my_rid: str, name_by_rid: dict[str, str]) -> list[str]:
    """我在队伍表里的角色与队友。"""
    lines = []
    for t in await SVC.teams.list_all():
        captain_ids = t.captain_ids
        member_ids = t.member_ids
        is_captain = my_rid in captain_ids
        is_member = my_rid in member_ids
        if not (is_captain or is_member):
            continue
        mates = [name_by_rid.get(r, "?") for r in captain_ids + member_ids if r != my_rid]
        role = "队长" if is_captain else "队员"
        tid = t.team_no or t.record_id
        lines.append(f"- **{tid}**（{role}）" + (f"：{'、'.join(mates)}" if mates else "：暂无其他成员"))
    return lines or ["- 未预组队（如需组队请联系管理员或等待统一分配）"]


async def _team_contains_me(team_rids: list[str], my_rid: str) -> bool:
    for t in await SVC.teams.list_all():
        if t.record_id not in team_rids:
            continue
        if my_rid in t.captain_ids + t.member_ids:
            return True
    return False


async def _project_lines(my_rid: str) -> list[str]:
    """我参与的项目与提交状态。"""
    lines = []
    for p in await SVC.projects.list_all():
        if my_rid not in p.member_ids:
            if not (p.team_ids and await _team_contains_me(p.team_ids, my_rid)):
                continue
        status = "✅ 已提交" if p.repo_url else "⏳ 未提交"
        lines.append(f"- **{p.project_no or '?'} {p.name or '未命名'}** —— {status}（{p.votes} 票）")
        if p.repo_url:
            lines.append(f"  仓库：{p.repo_url}")
        if p.demo_url:
            lines.append(f"  演示：{p.demo_url}")
    return lines or ["- 暂无参赛项目（项目表未登记你的项目）"]


async def handle_profile(ctx: MsgCtx) -> None:
    me = await SVC.contestants.get_by_open_id(ctx.open_id)
    org_me = await SVC.organizers.get_by_open_id(ctx.open_id)
    if me is None and org_me is None:
        await send_card(ctx.open_id, result_card("个人中心", False, [
            "你还未完成验证，先发「验证」或点帮助卡片里的「验证身份」。"]))
        return

    lines = []
    # 组委会身份块
    if org_me is not None:
        overified = org_me.verify_status or "未验证"
        lines += [
            f"**{org_me.name or '?'}**（{org_me.committee_identity}）",
            f"- 组委会验证：{'✅ ' if overified == '已验证' else '❌ '}{overified}",
        ]
    # 选手身份块
    if me is not None:
        audit = me.audit_status or "未审核"
        verified = me.verify_status or "未验证"
        if lines:
            lines.append("")
        name_by_rid = {c.record_id: c.name or c.contestant_no or "?"
                       for c in await SVC.contestants.list_all()}
        lines += [
            f"**{me.name or '?'}**（{me.contestant_no or '?'}）",
            f"- 验证状态：{'✅ ' if verified == '已验证' else '❌ '}{verified}",
            f"- 审核状态：{'✅ ' if audit == '审核通过' else '⏳ '}{audit}",
            "",
            "**我的队伍**",
            *(await _team_lines(me.record_id, name_by_rid)),
            "",
            "**项目提交状态**",
            *(await _project_lines(me.record_id)),
        ]
    await send_card(ctx.open_id, result_card("个人中心", True, lines))


class ProfilePlugin(Plugin):
    name = "profile"
    dependencies = ()

    def setup(self) -> None:
        """注册用户侧个人中心指令。"""
        REGISTRY.user_command("个人中心", "我的", plugin=self.name)(handle_profile)
