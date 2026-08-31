"""拉群模块：读取群配置表，把已验证选手拉入所有启用群，写拉群记录；支持补拉。"""
from __future__ import annotations

import logging

from ...core.base_store import BaseStore
from ...core.card_kit import result_card
from ...core.config import CFG
from ...core.lark_client import add_members, send_card, send_text
from ...core.registry import REGISTRY, MsgCtx
from ..sync import is_admin

log = logging.getLogger(__name__)


def enabled_chats(store: BaseStore) -> list[dict]:
    out = []
    for r in store.list_records(CFG.tbl_group_config):
        f = r.get("fields") or {}
        if f.get("启用") is True and f.get("chat_id"):
            out.append({"chat_id": str(f["chat_id"]).strip(), "群名": str(f.get("群名") or "")})
    return out


def pull_user_into_groups(users: list[dict]) -> tuple[int, str | None]:
    """把若干选手拉入所有启用群，写拉群记录。users: [{open_id, 选手ID, record_id}]"""
    store = BaseStore(CFG.db_base_token)
    chats = enabled_chats(store)
    ok_total, err = 0, None
    log_rows = []
    for chat in chats:
        ok, fail = add_members(chat["chat_id"], [u["open_id"] for u in users])
        ok_total += ok
        if fail:
            err = f"{chat['群名']}: {fail}"
        for u in users:
            log_rows.append({"fields": {
                "选手": [{"id": u["record_id"]}] if u.get("record_id") else [],
                "chat_id": chat["chat_id"],
                "群名": chat["群名"],
                "结果": "成功" if not fail else "失败",
                "失败原因": fail or "",
            }})
        _mark_pulled(store, chat["chat_id"], users, ok, fail)
    if log_rows:
        store.batch_create(CFG.tbl_pull_log, log_rows)
    return ok_total, err


def _mark_pulled(store: BaseStore, chat_id: str, users: list[dict], ok: int, fail: str | None) -> None:
    if fail:
        return  # 部分失败时不改状态，留给补拉
    items = []
    for u in users:
        if u.get("record_id"):
            items.append({"record_id": u["record_id"], "fields": {"入群状态": "已入群"}})
    if items:
        store.batch_update(CFG.tbl_contestants, items)


async def handle_backfill(ctx: MsgCtx) -> None:
    if ctx.chat_type == "p2p" and not is_admin(ctx.open_id):
        await send_text(ctx.open_id, "补拉指令仅限管理员使用。")
        return
    store = BaseStore(CFG.db_base_token)
    targets = []
    for r in store.list_records(CFG.tbl_contestants):
        f = r.get("fields") or {}
        if str(f.get("验证状态") or "") == "已验证" and str(f.get("入群状态") or "") != "已入群" and f.get("飞书open_id"):
            targets.append({"open_id": str(f["飞书open_id"]), "选手ID": str(f.get("选手ID") or ""),
                            "record_id": r["record_id"]})
    if not targets:
        await send_text(ctx.open_id, "没有需要补拉的选手。")
        return
    ok, fail = pull_user_into_groups(targets)
    await send_card(ctx.open_id, result_card("补拉完成", True, [
        f"- 待补拉选手：{len(targets)}", f"- 成功拉入群人次：{ok}",
        *( [f"- 失败：{fail}"] if fail else [] )]))


REGISTRY.command("补拉")(handle_backfill)
