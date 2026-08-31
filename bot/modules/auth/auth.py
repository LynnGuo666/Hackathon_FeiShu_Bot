"""验证授权模块：读取飞书账号手机号 -> 选手库比对 -> 绑定 open_id -> 触发拉群。"""
from __future__ import annotations

import logging

from ...core.card_kit import result_card
from ...core.config import CFG
from ...core.lark_client import get_user_phone, send_card, send_text
from ...core.registry import REGISTRY, MsgCtx
from ..group.group import pull_user_into_groups
from ..sync.sync import normalize_phone

log = logging.getLogger(__name__)


def lookup_contestant_by_phone(phone: str) -> dict | None:
    from ...core.base_store import BaseStore
    store = BaseStore(CFG.db_base_token)
    recs = store.list_records(CFG.tbl_contestants)
    for r in recs:
        if normalize_phone((r.get("fields") or {}).get("手机号")) == phone:
            return r
    return None


async def handle_verify(ctx: MsgCtx) -> None:
    if ctx.chat_type != "p2p":
        await send_text(ctx.open_id, "请私聊我发送「验证」进行授权，避免手机号信息暴露在群里。")
        return
    try:
        await send_text(ctx.open_id, "正在校验你的飞书账号手机号…")
        phone = get_user_phone(ctx.open_id)
    except RuntimeError as e:
        await send_card(ctx.open_id, result_card("验证失败", False, [
            f"无法读取你的手机号：{e}",
            "请联系管理员在选手表中人工补录你的飞书信息。"]))
        return
    if not phone:
        await send_card(ctx.open_id, result_card("验证失败", False, ["你的飞书账号未绑定手机号。"]))
        return

    rec = lookup_contestant_by_phone(normalize_phone(phone))
    if rec is None:
        await send_card(ctx.open_id, result_card("验证未通过", False, [
            f"手机号 `{phone}` 不在选手名单中。",
            "如果你已报名，请确认报名表中的手机号与本飞书账号一致，或联系管理员。"]))
        return

    fields = rec.get("fields") or {}
    status = str(fields.get("审核状态") or "").strip()
    if status != "审核通过":
        await send_card(ctx.open_id, result_card("验证未通过", False, [
            f"你好 **{fields.get('姓名', '')}**，你的报名当前状态为「{status or '未审核'}」。",
            "审核通过后再来验证即可自动拉入交流群。"]))
        return

    from ...core.base_store import BaseStore
    store = BaseStore(CFG.db_base_token)
    store.batch_update(CFG.tbl_contestants, [{"record_id": rec["record_id"], "fields": {
        "飞书open_id": ctx.open_id, "验证状态": "已验证"}}])
    name = str(fields.get("姓名") or "")
    cid = str(fields.get("选手ID") or "")
    ok, fail = pull_user_into_groups([{"open_id": ctx.open_id, "选手ID": cid, "record_id": rec["record_id"]}])
    lines = [f"欢迎 **{name}**（{cid}）！验证成功 ✅", ""]
    lines.append(f"- 已拉入 {ok} 个交流群" + (f"（{fail}）" if fail else ""))
    await send_card(ctx.open_id, result_card("验证成功", True, lines))


REGISTRY.command("验证", "授权")(handle_verify)
