"""验证授权模块。

双通道验证：
- 内部用户（同租户）：私聊发「验证」→ 自动读取飞书账号手机号比对（无感）。
- 外部用户（跨租户，黑客松选手大多是这类）：通讯录 API 读不到其手机号，
  改为回复授权卡片，用户填「手机号 + vx号」双因子，与选手库比对一致即通过。

通过后绑定 open_id、更新验证状态，并触发拉群。
"""
from __future__ import annotations

import logging

from ...core.card_kit import result_card
from ...core.config import CFG
from ...core.lark_client import get_user_phone, send_card, send_text
from ...core.registry import REGISTRY, CardCtx, MsgCtx
from ..group.group import pull_user_into_groups
from ..sync.sync import audit_status, is_valid_phone, normalize_phone

log = logging.getLogger(__name__)


def _load_contestants() -> list[dict]:
    """一次拉全量选手记录，流程内复用（避免多次全表扫描造成延迟）。"""
    from ...core.base_store import BaseStore
    return BaseStore(CFG.db_base_token).list_records(CFG.tbl_contestants)


def _find_by_phone(phone: str, contestants: list[dict] | None = None) -> dict | None:
    for r in (contestants if contestants is not None else _load_contestants()):
        if normalize_phone((r.get("fields") or {}).get("手机号")) == phone:
            return r
    return None


def _find_by_open_id(open_id: str, contestants: list[dict] | None = None) -> dict | None:
    for r in (contestants if contestants is not None else _load_contestants()):
        if str((r.get("fields") or {}).get("飞书open_id") or "") == open_id:
            return r
    return None


def _vx_matches(fields: dict, vx_input: str) -> bool:
    """vx号比对：去空格后全等或输入是 vx 号的后4位及以上尾部。"""
    real = str(fields.get("vx号") or "").strip()
    inp = vx_input.strip()
    if not real or not inp:
        return False
    if real == inp:
        return True
    return len(inp) >= 4 and real.endswith(inp)


def _approve(rec: dict, open_id: str) -> tuple[int, str | None, bool]:
    """绑定 open_id 并标记已验证；审核通过则拉群。返回 (拉群成功数, 失败信息, 是否已拉群)。"""
    from ...core.base_store import BaseStore
    store = BaseStore(CFG.db_base_token)
    store.batch_update(CFG.tbl_contestants, [{"record_id": rec["record_id"], "fields": {
        "飞书open_id": open_id, "验证状态": "已验证"}}])
    if audit_status(rec.get("fields") or {}) != "审核通过":
        return 0, None, False
    ok, fail = pull_user_into_groups([{"open_id": open_id, "选手ID": str(rec["fields"].get("选手ID") or ""),
                                       "record_id": rec["record_id"]}])
    return ok, fail, True


def _reply_result(open_id: str, rec: dict, ok: int, fail: str | None, pulled: bool = True) -> None:
    f = rec.get("fields") or {}
    name = str(f.get("姓名") or "")
    cid = str(f.get("选手ID") or "")
    if pulled:
        lines = [f"欢迎 **{name}**（{cid}）！验证成功 ✅", "", f"- 已拉入 {ok} 个交流群" + (f"（{fail}）" if fail else "")]
    else:
        lines = [f"**{name}**（{cid}），身份验证成功 ✅", "",
                 f"- 你的报名当前状态为「{audit_status(f) or '未审核'}」，审核通过后会自动拉你进入交流群。",
                 "- 无需重新验证，届时机器人会自动处理。"]
    from ...core.lark_client import send_card as _send_card
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send_card(open_id, result_card("验证成功", True, lines)))
    except RuntimeError:
        asyncio.run(_send_card(open_id, result_card("验证成功", True, lines)))


async def handle_verify(ctx: MsgCtx) -> None:
    if ctx.chat_type != "p2p":
        await send_text(ctx.open_id, "请私聊我发送「验证」进行授权，避免手机号信息暴露在群里。")
        return

    # 一次读取全量选手，后续查找复用
    contestants = _load_contestants()

    # 已验证用户直接提示
    bound = _find_by_open_id(ctx.open_id, contestants)
    if bound is not None:
        await send_card(ctx.open_id, result_card("已验证", True, [
            f"你已绑定选手 **{bound['fields'].get('姓名')}**（{bound['fields'].get('选手ID')}）。",
            "如需换绑请联系管理员。"]))
        return

    # 内部用户：尝试自动读取手机号（外部用户会 41050，自动降级到卡片表单）
    auto_phone = None
    try:
        auto_phone = get_user_phone(ctx.open_id)
    except RuntimeError:
        auto_phone = None

    if auto_phone and is_valid_phone(normalize_phone(auto_phone)):
        phone = normalize_phone(auto_phone)
        rec = _find_by_phone(phone, contestants)
        if rec is None:
            await send_card(ctx.open_id, result_card("验证未通过", False, [
                f"手机号 `{phone}` 不在选手名单中。",
                "如果你已报名，请确认报名表中的手机号与本飞书账号一致，或发送「验证」改用手动方式。"]))
            return
        # 验证 = 绑定身份，与审核解耦：未审核通过也绑定，审核通过后由自动补拉任务拉群
        ok, fail, pulled = _approve(rec, ctx.open_id)
        await _reply_result(ctx.open_id, rec, ok, fail, pulled)
        return

    # 外部用户 / 读不到手机号：回复授权卡片（手机号 + vx号 双因子）
    await send_card(ctx.open_id, verify_form_card())


def verify_form_card() -> dict:
    """授权表单卡片（JSON 2.0）：手机号 + vx号 双因子，form_submit 一次回调全部表单值。"""
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "选手授权验证"}, "template": "blue"},
        "body": {"elements": [
            {"tag": "markdown", "content":
                "无法自动读取你的手机号（企业外用户），请填写**报名表**中登记的信息完成验证："},
            {"tag": "form", "name": "verify_form",
             "elements": [
                 {"tag": "input", "name": "phone",
                  "placeholder": {"tag": "plain_text", "content": "报名手机号"},
                  "label": {"tag": "plain_text", "content": "手机号"},
                  "max_length": 11},
                 {"tag": "input", "name": "vx",
                  "placeholder": {"tag": "plain_text", "content": "报名时填写的 vx 号（或其后4位）"},
                  "label": {"tag": "plain_text", "content": "vx号"},
                  "max_length": 50},
                 {"tag": "button", "name": "submit",
                  "text": {"tag": "plain_text", "content": "提交验证"},
                  "type": "primary", "size": "medium",
                  "form_action_type": "submit", "action_type": "form_submit",
                  "value": {"action": "verify_submit"}},
             ]},
        ]},
    }


async def handle_verify_submit(card_ctx: CardCtx) -> None:
    """卡片表单提交：所有回执都原地更新原卡片，不发新消息。"""
    from ...core.lark_client import update_card

    async def reply(ok: bool, title: str, lines: list[str]) -> None:
        await update_card(card_ctx.token, result_card(title, ok, lines))

    form = card_ctx.form_value or {}
    phone = normalize_phone(str(form.get("phone") or ""))
    vx = str(form.get("vx") or "")
    if not is_valid_phone(phone):
        await reply(False, "验证未通过", ["手机号格式不正确，请填写 11 位大陆手机号。"])
        return
    contestants = _load_contestants()
    # 已验证用户直接提示，避免重复绑定
    bound = _find_by_open_id(card_ctx.open_id, contestants)
    if bound is not None:
        await reply(True, "已验证", [
            f"你已绑定选手 **{bound['fields'].get('姓名')}**（{bound['fields'].get('选手ID')}），无需重复验证。"])
        return
    rec = _find_by_phone(phone, contestants)
    if rec is None:
        await reply(False, "验证未通过", ["该手机号不在选手名单中。如果你已报名，请确认报名表中的手机号，或联系管理员。"])
        return
    f = rec.get("fields") or {}
    if not _vx_matches(f, vx):
        await reply(False, "验证未通过", ["vx号与报名信息不一致，请检查（可填报名时登记的 vx 号或其后 4 位）。"])
        return
    # 验证 = 绑定身份，与审核解耦：未审核通过也绑定，审核通过后由自动补拉任务拉群
    ok, fail, pulled = _approve(rec, card_ctx.open_id)
    name = str(f.get("姓名") or "")
    cid = str(f.get("选手ID") or "")
    if pulled:
        lines = [f"欢迎 **{name}**（{cid}）！验证成功 ✅", "", f"- 已拉入 {ok} 个交流群" + (f"（{fail}）" if fail else "")]
    else:
        lines = [f"**{name}**（{cid}），身份验证成功 ✅", "",
                 f"- 你的报名当前状态为「{audit_status(f) or '未审核'}」，审核通过后会自动拉你进入交流群。",
                 "- 无需重新验证，届时机器人会自动处理。"]
    await reply(True, "验证成功", lines)


REGISTRY.command("验证", "授权", "验证身份")(handle_verify)
REGISTRY.on_card("verify_submit")(handle_verify_submit)
