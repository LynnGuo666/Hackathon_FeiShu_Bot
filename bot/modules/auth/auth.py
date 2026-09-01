"""验证授权模块。

双通道验证，先选手后组委会：
- 选手：内部用户（同租户）私聊发「验证」→ 自动读取飞书账号手机号比对（无感）；
  外部用户（跨租户，黑客松选手大多是这类）→ 授权卡片填「手机号 + vx号」双因子。
- 组委会：选手表未命中时查组委会表，双因子为「手机号 + 姓名」
  （内部用户自动读手机号 + 需提供姓名；外部用户表单填手机号 + 姓名）。
  通过后按「组委会-{身份}」拉入对应群。

通过后绑定 open_id、更新验证状态，并触发拉群。

存储一律经 SVC 仓库接口（core.service），不直接接触飞书字段。
"""
from __future__ import annotations

import asyncio
import logging

from ...core.card_kit import result_card
from ...core.lark_client import get_user_phone, send_card, send_text
from ...core.models import Contestant, Organizer
from ...core.registry import REGISTRY, CardCtx, MsgCtx
from ...core.service import SVC
from ..group.group import ID_CONTESTANT, pull_user_into_groups
from ..sync.sync import is_valid_phone, normalize_phone

log = logging.getLogger(__name__)


def _vx_matches(vx_real: str, vx_input: str) -> bool:
    """vx号比对：去空格后全等或输入是 vx 号的后4位及以上尾部。"""
    real = (vx_real or "").strip()
    inp = vx_input.strip()
    if not real or not inp:
        return False
    if real == inp:
        return True
    return len(inp) >= 4 and real.endswith(inp)


def _name_matches(name_real: str, name_input: str) -> bool:
    """姓名比对：去空格全等。"""
    return bool(name_real) and name_real == name_input.strip()


async def _approve_contestant(c: Contestant, open_id: str) -> tuple[int, str | None, bool]:
    """绑定 open_id 并标记已验证；审核通过则拉群。返回 (拉群成功数, 失败信息, 是否已拉群)。"""
    await SVC.contestants.update(c.record_id, open_id=open_id, verify_status="已验证")
    if not c.approved:
        return 0, None, False
    ok, fail = await pull_user_into_groups(
        [{"open_id": open_id, "选手ID": c.contestant_no,
          "record_id": c.record_id, "identities": [ID_CONTESTANT]}])
    return ok, fail, True


async def _approve_organizer(o: Organizer, open_id: str) -> tuple[int, str | None]:
    """绑定 open_id、标记已验证，按「组委会-{身份}」拉群。返回 (成功数, 失败信息)。"""
    await SVC.organizers.update(o.record_id, open_id=open_id, verify_status="已验证")
    return await pull_user_into_groups(
        [{"open_id": open_id, "选手ID": o.name,
          "record_id": o.record_id, "identities": [o.committee_identity]}])


def _reply_result_lines(c: Contestant, ok: int, fail: str | None, pulled: bool) -> list[str]:
    if pulled:
        return [f"欢迎 **{c.name}**（{c.contestant_no}）！验证成功 ✅", "",
                f"- 已拉入 {ok} 个交流群" + (f"（{fail}）" if fail else "")]
    return [f"**{c.name}**（{c.contestant_no}），身份验证成功 ✅", "",
            f"- 你的报名当前状态为「{c.audit_status or '未审核'}」，审核通过后会自动拉你进入交流群。",
            "- 无需重新验证，届时机器人会自动处理。"]


def _reply_org_lines(o: Organizer, ok: int, fail: str | None) -> list[str]:
    return [f"欢迎 **{o.name}**（{o.committee_identity}）！验证成功 ✅", "",
            f"- 已拉入 {ok} 个组委会群" + (f"（{fail}）" if fail else "")]


async def handle_verify(ctx: MsgCtx) -> None:
    # 已验证选手直接提示
    bound = await SVC.contestants.get_by_open_id(ctx.open_id)
    if bound is not None:
        await send_card(ctx.open_id, result_card("已验证", True, [
            f"你已绑定选手 **{bound.name}**（{bound.contestant_no}）。",
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
        rec = await SVC.contestants.find_by_phone(phone)
        if rec is not None:
            # 验证 = 绑定身份，与审核解耦：未审核通过也绑定，审核通过后由自动补拉任务拉群
            ok, fail, pulled = await _approve_contestant(rec, ctx.open_id)
            await send_card(ctx.open_id, result_card("验证成功", True,
                                                     _reply_result_lines(rec, ok, fail, pulled)))
            return
        # 选手表未命中 -> 组委会表（手机号命中即内部组委会，姓名在表单/自动流程里都要求提供）
        org = await _find_org_by_phone(phone)
        if org is not None:
            await send_card(ctx.open_id, org_verify_form_card(auto_phone=phone))
            return
        await send_card(ctx.open_id, result_card("验证未通过", False, [
            f"手机号 `{phone}` 不在选手名单中。",
            "如果你已报名，请确认报名表中的手机号与本飞书账号一致，或发送「验证」改用手动方式。"]))
        return

    # 外部用户 / 读不到手机号：回复授权卡片（先选手后组委会，表单里二选一）
    await send_card(ctx.open_id, verify_form_card())


async def _find_org_by_phone(phone: str) -> Organizer | None:
    for o in await SVC.organizers.list_all():
        if normalize_phone(o.phone) == phone:
            return o
    return None


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
    # 已验证用户直接提示，避免重复绑定
    bound = await SVC.contestants.get_by_open_id(card_ctx.open_id)
    if bound is not None:
        await reply(True, "已验证", [
            f"你已绑定选手 **{bound.name}**（{bound.contestant_no}），无需重复验证。"])
        return
    rec = await SVC.contestants.find_by_phone(phone)
    if rec is None:
        # 选手未命中 -> 组委会（外部用户表单走组委会通道）
        org = await _find_org_by_phone(phone)
        if org is not None:
            if not _name_matches(org.name, str(form.get("vx") or "")):
                await reply(False, "验证未通过", ["组委会名单命中手机号，但姓名不一致；请填写组委会登记的姓名。"])
                return
            ok, fail = await _approve_organizer(org, card_ctx.open_id)
            await reply(True, "验证成功", _reply_org_lines(org, ok, fail))
            return
        await reply(False, "验证未通过", ["该手机号不在选手名单中。如果你已报名，请确认报名表中的手机号，或联系管理员。"])
        return
    if not _vx_matches(rec.vx, vx):
        await reply(False, "验证未通过", ["vx号与报名信息不一致，请检查（可填报名时登记的 vx 号或其后 4 位）。"])
        return
    # 验证 = 绑定身份，与审核解耦：未审核通过也绑定，审核通过后由自动补拉任务拉群
    ok, fail, pulled = await _approve_contestant(rec, card_ctx.open_id)
    await reply(True, "验证成功", _reply_result_lines(rec, ok, fail, pulled))


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
    bound = await SVC.organizers.get_by_open_id(card_ctx.open_id)
    if bound is not None:
        await reply(True, "已验证", [
            f"你已绑定组委会成员 **{bound.name}**（{bound.committee_identity}），无需重复验证。"])
        return
    org = await _find_org_by_phone(phone)
    if org is None:
        await reply(False, "验证未通过", ["该手机号不在组委会名单中。如你是选手请用选手验证；如有疑问请联系管理员。"])
        return
    if not _name_matches(org.name, name):
        await reply(False, "验证未通过", ["姓名与组委会登记信息不一致，请检查。"])
        return
    ok, fail = await _approve_organizer(org, card_ctx.open_id)
    await reply(True, "验证成功", _reply_org_lines(org, ok, fail))


async def handle_bind(ctx: MsgCtx) -> None:
    """「绑定 <码>」：已验证用户用组委会绑定码额外绑定组委会身份（如以选手身份报名的导师）。

    - 未验证用户提示先验证；
    - 绑定码命中组委会记录且该记录未绑定其他 open_id 时：绑定 open_id、标记已验证，
      按「组委会-{身份}」拉群；选手身份保持不变（双重身份）。
    """
    parts = ctx.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await send_text(ctx.open_id, "用法：绑定 <绑定码>（绑定码由组委会创建名单时生成，可用「生成绑定码」指令批量补齐）")
        return
    code = parts[1].strip()

    org = await SVC.organizers.get_by_binding_code(code)
    if org is None:
        await send_card(ctx.open_id, result_card("绑定失败", False, ["绑定码不存在，请检查（区分大小写）。"]))
        return

    # 该组委会记录已被其他账号绑定
    if org.open_id and org.open_id != ctx.open_id:
        await send_card(ctx.open_id, result_card("绑定失败", False, [
            f"绑定码 `{code}`（{org.name}）已被其他账号绑定，如需换绑请联系管理员。"]))
        return

    # 已绑定过同一记录：幂等提示
    if org.open_id == ctx.open_id:
        await send_card(ctx.open_id, result_card("已绑定", True, [
            f"你已绑定组委会身份 **{org.committee_identity}**（{org.name}），无需重复绑定。"]))
        return

    ok, fail = await _approve_organizer(org, ctx.open_id)
    await send_card(ctx.open_id, result_card("绑定成功", True, [
        f"已为你额外绑定组委会身份 **{org.committee_identity}**（{org.name}）✅", "",
        f"- 已拉入 {ok} 个组委会群" + (f"（{fail}）" if fail else ""),
        "- 你的选手身份不受影响。"]))


def register() -> None:
    """注册用户侧验证、绑定指令和表单回调。"""
    REGISTRY.user_command("验证", "授权", "验证身份")(handle_verify)
    REGISTRY.on_card("verify_submit", scope="user")(handle_verify_submit)
    REGISTRY.on_card("org_verify_submit", scope="user")(handle_org_verify_submit)
    REGISTRY.user_command("绑定")(handle_bind)
