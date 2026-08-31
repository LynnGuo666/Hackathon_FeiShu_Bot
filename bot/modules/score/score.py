"""积分模块：积分表写流水 + 选手表「积分」总分同步更新。

- add_score(contestant_record_id, delta, reason)：写一条流水并把选手表总分 ±delta；
- 「查分」指令：查看自己的积分与最近流水；管理员可「加分 @不适用」暂不支持，直接改表即可。
"""
from __future__ import annotations

import logging

from ...core.base_store import BaseStore
from ...core.card_kit import result_card
from ...core.config import CFG
from ...core.lark_client import send_card
from ...core.registry import REGISTRY, MsgCtx

log = logging.getLogger(__name__)


def _find_me(store: BaseStore, open_id: str) -> dict | None:
    for r in store.list_records(CFG.tbl_contestants):
        if str((r.get("fields") or {}).get("飞书open_id") or "") == open_id:
            return r
    return None


def add_score(contestant_record_id: str, delta: int, reason: str) -> int:
    """写积分流水并更新选手表总分，返回变动后总分。"""
    store = BaseStore(CFG.db_base_token)
    # 读当前总分
    total = 0
    for r in store.list_records(CFG.tbl_contestants):
        if r["record_id"] == contestant_record_id:
            total = int((r.get("fields") or {}).get("积分") or 0)
            break
    new_total = total + delta
    store.batch_create(CFG.tbl_score, [{
        "选手": [{"id": contestant_record_id}], "变动分值": delta,
        "变动后积分": new_total, "事由": reason}])
    store.batch_update(CFG.tbl_contestants, [{"record_id": contestant_record_id,
                                              "fields": {"积分": new_total}}])
    return new_total


async def handle_my_score(ctx: MsgCtx) -> None:
    store = BaseStore(CFG.db_base_token)
    me = _find_me(store, ctx.open_id)
    if me is None:
        await send_card(ctx.open_id, result_card("我的积分", False, ["你还未完成验证，先发「验证」。"]))
        return
    f = me.get("fields") or {}
    name = str(f.get("姓名") or "?")
    cid = str(f.get("选手ID") or "?")
    total = int(f.get("积分") or 0)
    lines = [f"**{name}**（{cid}）当前积分：**{total}**", "", "**最近流水**"]
    recent = []
    for r in store.list_records(CFG.tbl_score):
        rf = r.get("fields") or {}
        ids = [x.get("id") for x in rf.get("选手", []) if isinstance(x, dict)] if isinstance(rf.get("选手"), list) else []
        if me["record_id"] in ids:
            recent.append((str(rf.get("事由") or ""), int(rf.get("变动分值") or 0)))
    if not recent:
        lines.append("- 暂无积分记录")
    else:
        for reason, delta in recent[-5:][::-1]:
            lines.append(f"- {'+' if delta >= 0 else ''}{delta}　{reason}")
    await send_card(ctx.open_id, result_card("我的积分", True, lines))


def register() -> None:
    """注册用户侧积分查询指令。"""
    REGISTRY.user_command("查分", "积分")(handle_my_score)
