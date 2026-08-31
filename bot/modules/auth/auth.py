"""验证授权模块。

双通道验证，先选手后组委会：
- 选手：内部用户（同租户）私聊发「验证」→ 自动读取飞书账号手机号比对（无感）；
  外部用户（跨租户，黑客松选手大多是这类）→ 授权卡片填「手机号 + vx号」双因子。
- 组委会：选手表未命中时查组委会表，双因子为「手机号 + 姓名」
  （内部用户自动读手机号 + 需提供姓名；外部用户表单填手机号 + 姓名）。
  通过后按「组委会-{身份}」拉入对应群。

通过后绑定 open_id、更新验证状态，并触发拉群。
"""
from __future__ import annotations

import logging

from ...core.card_kit import result_card
from ...core.config import CFG
from ...core.lark_client import get_user_phone, send_card, send_text
from ...core.registry import REGISTRY, CardCtx, MsgCtx
from ..group.group import ID_CONTESTANT, pull_user_into_groups
from ..sync.sync import audit_status, is_valid_phone, normalize_phone

log = logging.getLogger(__name__)


def _load_contestants() -> list[dict]:
    """一次拉全量选手记录，流程内复用（避免多次全表扫描造成延迟）。"""
    from ...core.base_store import BaseStore
    return BaseStore(CFG.db_base_token).list_records(CFG.tbl_contestants)


def _load_organizers() -> list[dict]:
    from ...core.base_store import BaseStore
    return BaseStore(CFG.db_base_token).list_records(CFG.tbl_organizers)


def _text(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, list):
        return "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in cell).strip()
    return str(cell).strip()


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


def _find_org_by_phone(phone: str, organizers: list[dict] | None = None) -> dict | None:
    for r in (organizers if organizers is not None else _load_organizers()):
        if normalize_phone((r.get("fields") or {}).get("手机号")) == phone:
            return r
    return None


def _find_org_by_open_id(open_id: str, organizers: list[dict] | None = None) -> dict | None:
    for r in (organizers if organizers is not None else _load_organizers()):
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


def _name_matches(fields: dict, name_input: str) -> bool:
    """姓名比对：去空格全等。"""
    real = _text(fields.get("姓名"))
    return bool(real) and real == name_input.strip()


def _approve_contestant(rec: dict, open_id: str) -> tuple[int, str | None, bool]:
    """绑定 open_id 并标记已验证；审核通过则拉群。返回 (拉群成功数, 失败信息, 是否已拉群)。"""
    from ...core.base_store import BaseStore
    store = BaseStore(CFG.db_base_token)
    store.batch_update(CFG.tbl_contestants, [{"record_id": rec["record_id"], "fields": {
        "飞书open_id": open_id, "验证状态": "已验证"}}])
    if audit_status(rec.get("fields") or {}) != "审核通过":
        return 0, None, False
    ok, fail = pull_user_into_groups([{"open_id": open_id, "选手ID": str(rec["fields"].get("选手ID") or ""),
                                       "record_id": rec["record_id"], "identities": [ID_CONTESTANT]}])
    return ok, fail, True


def _approve_organizer(rec: dict, open_id: str) -> tuple[int, str | None]:
    """绑定 open_id、标记已验证，按「组委会-{身份}」拉群。返回 (成功数, 失败信息)。"""
    from ...core.base_store import BaseStore
    store = BaseStore(CFG.db_base_token)
    store.batch_update(CFG.tbl_organizers, [{"record_id": rec["record_id"], "fields": {
        "飞书open_id": open_id, "验证状态": "已验证"}}])
    ident = f"组委会-{_text((rec.get('fields') or {}).get('身份')) or '主办方'}"
    return pull_user_into_groups([{"open_id": open_id, "选手ID": _text((rec.get("fields") or {}).get("姓名")),
                                   "record_id": rec["record_id"], "identities": [ident]}])


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


def _reply_org_result(open_id: str, rec: dict, ok: int, fail: str | None) -> None:
    f = rec.get("fields") or {}
    name = _text(f.get("姓名"))
    ident = _text(f.get("身份")) or "主办方"
    lines = [f"欢迎 **{name}**（组委会-{ident}）！验证成功 ✅", "",
             f"- 已拉入 {ok} 个组委会群" + (f"（{fail}）" if fail else "")]
    import asyncio
    from ...core.lark_client import send_card as _send_card
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send_card(open_id, result_card("验证成功", True, lines)))
    except RuntimeError:
        asyncio.run(_send_card(open_id, result_card("验证成功", True, lines)))


async def handle_verify(ctx: MsgCtx) -> None:
    # 一次读取全量选手，后续查找复用
    contestants = _load_contestants()

    # 已验证选手直接提示
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
        if rec is not None:
            # 验证 = 绑定身份，与审核解耦：未审核通过也绑定，审核通过后由自动补拉任务拉群
            ok, fail, pulled = _approve_contestant(rec, ctx.open_id)
            await _reply_result(ctx.open_id, rec, ok, fail, pulled)
            return
        # 选手表未命中 -> 组委会表（手机号命中即内部组委会，姓名在表单/自动流程里都要求提供）
        org = _find_org_by_phone(phone)
        if org is not None:
            await send_card(ctx.open_id, org_verify_form_card(auto_phone=phone))
            return
        await send_card(ctx.open_id, result_card("验证未通过", False, [
            f"手机号 `{phone}` 不在选手名单中。",
            "如果你已报名，请确认报名表中的手机号与本飞书账号一致，或发送「验证」改用手动方式。"]))
        return

    # 外部用户 / 读不到手机号：回复授权卡片（先选手后组委会，表单里二选一）
    await send_card(ctx.open_id, verify_form_card())


def verify_form_card() -> dict:
    """选手授权表单卡片（JSON 2.0）：手机号 + vx号 双因子，form_submit 一次回调全部表单值。"""
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
                  "form_action_type": "submit",
                  "behaviors": [{"type": "callback", "value": {"action": "verify_submit"}}]},
             ]},
        ]},
    }


def org_verify_form_card(auto_phone: str = "") -> dict:
    """组委会授权表单卡片：手机号 + 姓名双因子。auto_phone 非空时预填并只读提示。"""
    phone_hint = f"（已自动读取：{auto_phone}）" if auto_phone else ""
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "组委会授权验证"}, "template": "turquoise"},
        "body": {"elements": [
            {"tag": "markdown", "content":
                "你是**组委会成员**（手机号在组委会名单中）。请填写组委会登记的信息完成验证：" + phone_hint},
            {"tag": "form", "name": "org_verify_form",
             "elements": [
                 {"tag": "input", "name": "phone",
                  "placeholder": {"tag": "plain_text", "content": "组委会登记手机号"},
                  "label": {"tag": "plain_text", "content": "手机号"},
                  "max_length": 11},
                 {"tag": "input", "name": "name",
                  "placeholder": {"tag": "plain_text", "content": "组委会登记的姓名"},
                  "label": {"tag": "plain_text", "content": "姓名"},
                  "max_length": 20},
                 {"tag": "button", "name": "submit",
                  "text": {"tag": "plain_text", "content": "提交验证"},
                  "type": "primary", "size": "medium",
                  "form_action_type": "submit",
                  "behaviors": [{"type": "callback", "value": {"action": "org_verify_submit"}}]},
             ]},
        ]},
    }


async def handle_verify_submit(card_ctx: CardCtx) -> None:
    """选手表单提交：所有回执都原地更新原卡片，不发新消息。"""
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
        # 选手未命中 -> 组委会（外部用户表单走组委会通道）
        org = _find_org_by_phone(phone)
        if org is not None:
            f = org.get("fields") or {}
            if not _name_matches(f, str(form.get("vx") or "")):
                await reply(False, "验证未通过", ["组委会名单命中手机号，但姓名不一致；请填写组委会登记的姓名。"])
                return
            ok, fail = _approve_organizer(org, card_ctx.open_id)
            name = _text(f.get("姓名"))
            ident = _text(f.get("身份")) or "主办方"
            await reply(True, "验证成功", [
                f"欢迎 **{name}**（组委会-{ident}）！验证成功 ✅", "",
                f"- 已拉入 {ok} 个组委会群" + (f"（{fail}）" if fail else "")])
            return
        await reply(False, "验证未通过", ["该手机号不在选手名单中。如果你已报名，请确认报名表中的手机号，或联系管理员。"])
        return
    f = rec.get("fields") or {}
    if not _vx_matches(f, vx):
        await reply(False, "验证未通过", ["vx号与报名信息不一致，请检查（可填报名时登记的 vx 号或其后 4 位）。"])
        return
    # 验证 = 绑定身份，与审核解耦：未审核通过也绑定，审核通过后由自动补拉任务拉群
    ok, fail, pulled = _approve_contestant(rec, card_ctx.open_id)
    name = str(f.get("姓名") or "")
    cid = str(f.get("选手ID") or "")
    if pulled:
        lines = [f"欢迎 **{name}**（{cid}）！验证成功 ✅", "", f"- 已拉入 {ok} 个交流群" + (f"（{fail}）" if fail else "")]
    else:
        lines = [f"**{name}**（{cid}），身份验证成功 ✅", "",
                 f"- 你的报名当前状态为「{audit_status(f) or '未审核'}」，审核通过后会自动拉你进入交流群。",
                 "- 无需重新验证，届时机器人会自动处理。"]
    await reply(True, "验证成功", lines)


async def handle_org_verify_submit(card_ctx: CardCtx) -> None:
    """组委会表单提交：手机号 + 姓名双因子，原地更新卡片。"""
    from ...core.lark_client import update_card

    async def reply(ok: bool, title: str, lines: list[str]) -> None:
        await update_card(card_ctx.token, result_card(title, ok, lines))

    form = card_ctx.form_value or {}
    phone = normalize_phone(str(form.get("phone") or ""))
    name = str(form.get("name") or "")
    if not is_valid_phone(phone):
        await reply(False, "验证未通过", ["手机号格式不正确，请填写 11 位大陆手机号。"])
        return
    organizers = _load_organizers()
    bound = _find_org_by_open_id(card_ctx.open_id, organizers)
    if bound is not None:
        f = bound.get("fields") or {}
        await reply(True, "已验证", [
            f"你已绑定组委会成员 **{_text(f.get('姓名'))}**（组委会-{_text(f.get('身份')) or '主办方'}），无需重复验证。"])
        return
    org = _find_org_by_phone(phone, organizers)
    if org is None:
        await reply(False, "验证未通过", ["该手机号不在组委会名单中。如你是选手请用选手验证；如有疑问请联系管理员。"])
        return
    f = org.get("fields") or {}
    if not _name_matches(f, name):
        await reply(False, "验证未通过", ["姓名与组委会登记信息不一致，请检查。"])
        return
    ok, fail = _approve_organizer(org, card_ctx.open_id)
    ident = _text(f.get("身份")) or "主办方"
    await reply(True, "验证成功", [
        f"欢迎 **{_text(f.get('姓名'))}**（组委会-{ident}）！验证成功 ✅", "",
        f"- 已拉入 {ok} 个组委会群" + (f"（{fail}）" if fail else "")])


async def handle_bind(ctx: MsgCtx) -> None:
    """「绑定 <码>」：已验证用户用组委会绑定码额外绑定组委会身份（如以选手身份报名的导师）。

    - 未验证用户提示先验证；
    - 绑定码命中组委会记录且该记录未绑定其他 open_id 时：绑定 open_id、标记已验证，
      按「组委会-{身份}」拉群；选手身份保持不变（双重身份）。
    """
    from ...core.base_store import BaseStore
    parts = ctx.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await send_text(ctx.open_id, "用法：绑定 <绑定码>（绑定码由组委会创建名单时生成，可用「生成绑定码」指令批量补齐）")
        return
    code = parts[1].strip()
    store = BaseStore(CFG.db_base_token)

    # 命中绑定码
    org = None
    for r in store.list_records(CFG.tbl_organizers):
        if str((r.get("fields") or {}).get("绑定码") or "").strip() == code:
            org = r
            break
    if org is None:
        await send_card(ctx.open_id, result_card("绑定失败", False, ["绑定码不存在，请检查（区分大小写）。"]))
        return
    f = org.get("fields") or {}
    name = _text(f.get("姓名"))
    ident = _text(f.get("身份")) or "主办方"

    # 该组委会记录已被其他账号绑定
    bound_oid = str(f.get("飞书open_id") or "")
    if bound_oid and bound_oid != ctx.open_id:
        await send_card(ctx.open_id, result_card("绑定失败", False, [
            f"绑定码 `{code}`（{name}）已被其他账号绑定，如需换绑请联系管理员。"]))
        return

    # 已绑定过同一记录：幂等提示
    if bound_oid == ctx.open_id:
        await send_card(ctx.open_id, result_card("已绑定", True, [
            f"你已绑定组委会身份 **{ident}**（{name}），无需重复绑定。"]))
        return

    ok, fail = _approve_organizer(org, ctx.open_id)
    await send_card(ctx.open_id, result_card("绑定成功", True, [
        f"已为你额外绑定组委会身份 **{ident}**（{name}）✅", "",
        f"- 已拉入 {ok} 个组委会群" + (f"（{fail}）" if fail else ""),
        "- 你的选手身份不受影响。"]))


def register() -> None:
    """注册用户侧验证、绑定指令和表单回调。"""
    REGISTRY.user_command("验证", "授权", "验证身份")(handle_verify)
    REGISTRY.on_card("verify_submit", scope="user")(handle_verify_submit)
    REGISTRY.on_card("org_verify_submit", scope="user")(handle_org_verify_submit)
    REGISTRY.user_command("绑定")(handle_bind)
