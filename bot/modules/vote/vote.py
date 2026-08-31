"""投票模块：决赛投票环节。

- 选手私聊发「投票」→ 机器人回项目列表卡片（每个项目一个投票按钮）
- 点按钮 → card.action.trigger 回调 → 校验（已验证选手、未投过票）→ 写投票表 + 项目表票数 +1
- 「票数」指令：查看当前票榜；「开票/关票」：管理员开关投票
"""
from __future__ import annotations

import logging

from ...core.base_store import BaseStore
from ...core.card_kit import result_card
from ...core.config import CFG
from ...core.lark_client import send_card, send_text
from ...core.registry import REGISTRY, CardCtx, MsgCtx

log = logging.getLogger(__name__)

# 投票开关（内存态；重启后默认开启，可用关票指令关闭）
vote_open = True


def list_projects(store: BaseStore) -> list[dict]:
    out = []
    for r in store.list_records(CFG.tbl_project):
        f = r.get("fields") or {}
        out.append({"record_id": r["record_id"],
                    "项目ID": str(f.get("项目ID") or ""),
                    "项目名称": str(f.get("项目名称") or "未命名"),
                    "票数": int(f.get("票数") or 0)})
    out.sort(key=lambda x: x["项目ID"])
    return out


def _link_ids(cell) -> list[str]:
    """link 字段读回形态是 [{"id": "rec..."}]，取出目标 record_id 列表。"""
    if not isinstance(cell, list):
        return []
    return [x["id"] for x in cell if isinstance(x, dict) and x.get("id")]


def _voted_projects(store: BaseStore, contestant_record_id: str) -> set[str]:
    voted = set()
    for r in store.list_records(CFG.tbl_vote):
        f = r.get("fields") or {}
        if contestant_record_id in _link_ids(f.get("投票人")):
            voted.update(_link_ids(f.get("项目")))
    return voted


def _find_contestant(store: BaseStore, open_id: str) -> dict | None:
    for r in store.list_records(CFG.tbl_contestants):
        if str((r.get("fields") or {}).get("飞书open_id") or "") == open_id:
            return r
    return None


def project_list_card() -> dict:
    """项目列表卡片（JSON 2.0）：每个项目一个投票按钮（behaviors callback）。票数不公开。"""
    store = BaseStore(CFG.db_base_token)
    projects = list_projects(store)
    elements = []
    if not projects:
        elements.append({"tag": "markdown", "content": "项目表暂无项目。"})
    for p in projects:
        elements.append({"tag": "markdown",
                         "content": f"**{p['项目ID']} {p['项目名称']}**"})
        elements.append({"tag": "button",
                         "text": {"tag": "plain_text", "content": f"投给 {p['项目名称']}"},
                         "type": "primary", "size": "medium",
                         "behaviors": [{"type": "callback",
                                        "value": {"action": "vote",
                                                  "project_record_id": p["record_id"]}}]})
    return {"schema": "2.0",
            "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "决赛投票"}, "template": "violet"},
            "body": {"elements": elements}}


async def handle_vote(ctx: MsgCtx) -> None:
    global vote_open
    if ctx.chat_type != "p2p":
        await send_text(ctx.open_id, "请私聊我发送「投票」，一人一票。")
        return
    if not vote_open:
        await send_card(ctx.open_id, result_card("投票未开放", False, ["投票通道当前已关闭，请等待主持人开票。"]))
        return
    store = BaseStore(CFG.db_base_token)
    me = _find_contestant(store, ctx.open_id)
    if me is None:
        await send_card(ctx.open_id, result_card("无法投票", False, ["你还未完成验证，请先发送「验证」。"]))
        return
    await send_card(ctx.open_id, project_list_card())


async def handle_vote_action(card_ctx: CardCtx) -> None:
    global vote_open
    if not vote_open:
        await send_text(card_ctx.open_id, "投票通道已关闭。")
        return
    store = BaseStore(CFG.db_base_token)
    me = _find_contestant(store, card_ctx.open_id)
    if me is None:
        await send_text(card_ctx.open_id, "你还未完成验证，无法投票。")
        return
    project_rid = card_ctx.raw.get("action", {}).get("value", {}).get("project_record_id", "")
    if not project_rid:
        return
    if project_rid in _voted_projects(store, me["record_id"]):
        await send_text(card_ctx.open_id, "你已经投过票了，一人一票哦。")
        return
    store.batch_create(CFG.tbl_vote, [{"fields": {
        "投票人": [{"id": me["record_id"]}], "项目": [{"id": project_rid}]}}])
    # 项目表票数 +1（票数不公开，不回传具体数字）
    for p in store.list_records(CFG.tbl_project):
        if p["record_id"] == project_rid:
            cur = int((p.get("fields") or {}).get("票数") or 0)
            store.batch_update(CFG.tbl_project, [{"record_id": project_rid, "fields": {"票数": cur + 1}}])
            name = str((p.get("fields") or {}).get("项目名称") or "")
            await send_text(card_ctx.open_id, f"投票成功 ✅ 你把票投给了「{name}」。")
            break


async def handle_votes_board(ctx: MsgCtx) -> None:
    """票榜：票数不公开，只展示排名不带数字（管理员可见票数）。"""
    from ..sync import is_admin
    store = BaseStore(CFG.db_base_token)
    projects = sorted(list_projects(store), key=lambda x: -x["票数"])
    show_votes = is_admin(ctx.open_id)
    lines = ["**当前票榜**", ""]
    for i, p in enumerate(projects, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} **{p['项目名称']}**" + (f" —— {p['票数']} 票" if show_votes else ""))
    if not show_votes:
        lines += ["", "票数暂不公开，最终结果以主办方公布为准。"]
    await send_card(ctx.open_id, result_card("票榜", True, lines))


async def handle_vote_toggle(ctx: MsgCtx) -> None:
    from ..sync import is_admin
    if not is_admin(ctx.open_id):
        await send_text(ctx.open_id, "该指令仅限管理员使用。")
        return
    global vote_open
    vote_open = ctx.text in ("开票",)
    state = "开放" if vote_open else "关闭"
    await send_text(ctx.open_id, f"投票通道已{state}。")


REGISTRY.command("投票")(handle_vote)
REGISTRY.command("票数", "票榜")(handle_votes_board)
REGISTRY.command("开票", "关票")(handle_vote_toggle)
REGISTRY.on_card("vote")(handle_vote_action)
