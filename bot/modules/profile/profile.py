"""个人中心模块：展示选手唯一 ID、队友信息、项目提交状态。

- 「个人中心」/「我的」指令或帮助卡按钮触发；
- 未验证用户提示先验证；
- 已验证用户展示：选手 ID、姓名、审核/验证状态、队伍（队长/队友）、项目提交状态。
"""
from __future__ import annotations

import logging

from ...core.base_store import BaseStore
from ...core.card_kit import result_card
from ...core.config import CFG
from ...core.lark_client import send_card
from ...core.registry import REGISTRY, MsgCtx

log = logging.getLogger(__name__)


def _link_ids(cell) -> list[str]:
    """link 字段读回形态是 [{"id": ...}] 或 [{"record_ids": [...]}]，取出目标 record_id 列表。"""
    out: list[str] = []
    if not isinstance(cell, list):
        return out
    for x in cell:
        if not isinstance(x, dict):
            continue
        if x.get("id"):
            out.append(str(x["id"]))
        for rid in (x.get("record_ids") or []):
            if rid:
                out.append(str(rid))
    return out


def _text(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, list):
        return "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in cell).strip()
    return str(cell).strip()


def _find_me(store: BaseStore, open_id: str) -> dict | None:
    for r in store.list_records(CFG.tbl_contestants):
        if str((r.get("fields") or {}).get("飞书open_id") or "") == open_id:
            return r
    return None


def _team_lines(store: BaseStore, my_rid: str, name_by_rid: dict[str, str]) -> list[str]:
    """我在队伍表里的角色与队友。"""
    lines = []
    for t in store.list_records(CFG.tbl_teams):
        f = t.get("fields") or {}
        captain_ids = _link_ids(f.get("队长"))
        member_ids = _link_ids(f.get("队友"))
        is_captain = my_rid in captain_ids
        is_member = my_rid in member_ids
        if not (is_captain or is_member):
            continue
        mates = [name_by_rid.get(r, "?") for r in captain_ids + member_ids if r != my_rid]
        role = "队长" if is_captain else "队员"
        tid = _text(f.get("队伍ID")) or t["record_id"]
        lines.append(f"- **{tid}**（{role}）" + (f"：{'、'.join(mates)}" if mates else "：暂无其他成员"))
    return lines or ["- 未预组队（如需组队请联系管理员或等待统一分配）"]


def _project_lines(store: BaseStore, my_rid: str, name_by_rid: dict[str, str]) -> list[str]:
    """我参与的项目与提交状态。"""
    lines = []
    for p in store.list_records(CFG.tbl_project):
        f = p.get("fields") or {}
        member_ids = _link_ids(f.get("成员"))
        team_ids = _link_ids(f.get("队伍"))
        if my_rid not in member_ids and not (team_ids and _team_contains_me(store, team_ids, my_rid)):
            continue
        pid = _text(f.get("项目ID")) or "?"
        pname = _text(f.get("项目名称")) or "未命名"
        repo = _text(f.get("仓库链接"))
        demo = _text(f.get("演示链接"))
        votes = _text(f.get("票数")) or "0"
        # 提交状态：有仓库链接视为已提交
        status = "✅ 已提交" if repo else "⏳ 未提交"
        lines.append(f"- **{pid} {pname}** —— {status}（{votes} 票）")
        if repo:
            lines.append(f"  仓库：{repo}")
        if demo:
            lines.append(f"  演示：{demo}")
    return lines or ["- 暂无参赛项目（项目表未登记你的项目）"]


def _team_contains_me(store: BaseStore, team_rids: list[str], my_rid: str) -> bool:
    for t in store.list_records(CFG.tbl_teams):
        if t["record_id"] not in team_rids:
            continue
        if my_rid in _link_ids((t.get("fields") or {}).get("队长")) + _link_ids((t.get("fields") or {}).get("队友")):
            return True
    return False


async def handle_profile(ctx: MsgCtx) -> None:
    store = BaseStore(CFG.db_base_token)
    me = _find_me(store, ctx.open_id)
    if me is None:
        await send_card(ctx.open_id, result_card("个人中心", False, [
            "你还未完成验证，先发「验证」或点帮助卡片里的「验证身份」。"]))
        return
    f = me.get("fields") or {}
    my_rid = me["record_id"]
    name = _text(f.get("姓名")) or "?"
    cid = _text(f.get("选手ID")) or "?"
    audit = _text(f.get("审核状态")) or "未审核"
    verified = _text(f.get("验证状态")) or "未验证"

    name_by_rid = {}
    for r in store.list_records(CFG.tbl_contestants):
        rf = r.get("fields") or {}
        name_by_rid[r["record_id"]] = _text(rf.get("姓名")) or _text(rf.get("选手ID")) or "?"

    lines = [
        f"**{name}**（{cid}）",
        f"- 验证状态：{'✅ ' if verified == '已验证' else '❌ '}{verified}",
        f"- 审核状态：{'✅ ' if audit == '审核通过' else '⏳ '}{audit}",
        "",
        "**我的队伍**",
        *_team_lines(store, my_rid, name_by_rid),
        "",
        "**项目提交状态**",
        *_project_lines(store, my_rid, name_by_rid),
    ]
    await send_card(ctx.open_id, result_card("个人中心", True, lines))


REGISTRY.command("个人中心", "我的")(handle_profile)
