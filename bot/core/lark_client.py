"""飞书开放平台客户端封装（消息、联系人等，bot 身份，走 lark-oapi SDK）。

async 接口内部用 ``asyncio.to_thread`` 包装同步 SDK 调用，避免阻塞事件循环
（SDK 回调线程若超时不 ACK，飞书会重推事件）。
"""
from __future__ import annotations

from collections.abc import Mapping
import asyncio
import json
import os
import random
import threading
import time

from .config import CFG

_CLIENT = None


# `moderation_setting` belongs to /moderation and is applied separately below.
_CHAT_RESTRICTION_FIELDS = (
    "add_member_permission",
    "share_card_permission",
    "at_all_permission",
    "edit_permission",
    "join_message_visibility",
    "leave_message_visibility",
    "membership_approval",
    "urgent_setting",
    "video_conference_setting",
    "hide_member_count_setting",
)

# The old player group is the baseline for all groups created by the bot.
DEFAULT_GROUP_RESTRICTIONS: dict[str, str] = {
    "add_member_permission": "only_owner",
    "share_card_permission": "not_allowed",
    "at_all_permission": "only_owner",
    "edit_permission": "only_owner",
    "join_message_visibility": "all_members",
    "leave_message_visibility": "only_owner",
    "membership_approval": "approval_required",
    "moderation_setting": "all_members",
    "urgent_setting": "only_owner",
    "video_conference_setting": "only_owner",
    "hide_member_count_setting": "all_members",
}

# Presets retained for groups that intentionally follow a different old-group
# profile. They are also useful when migrating an existing group once.
NOTIFICATION_GROUP_RESTRICTIONS: dict[str, str] = {
    **DEFAULT_GROUP_RESTRICTIONS,
    "join_message_visibility": "not_anyone",
    "leave_message_visibility": "not_anyone",
    "membership_approval": "no_approval_required",
    "moderation_setting": "only_owner",
    "urgent_setting": "all_members",
    "video_conference_setting": "all_members",
}

COMMITTEE_GROUP_RESTRICTIONS: dict[str, str] = {
    **DEFAULT_GROUP_RESTRICTIONS,
    "join_message_visibility": "not_anyone",
    "leave_message_visibility": "not_anyone",
}


def client():
    global _CLIENT
    if _CLIENT is None:
        import lark_oapi as lark
        if not CFG.has_app_credentials:
            raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请先配置 .env")
        _CLIENT = lark.Client.builder().app_id(CFG.app_id).app_secret(CFG.app_secret).build()
    return _CLIENT


# ---------- IM/通讯录 API 限流 + 重试 ----------
# 消息发送、群成员、通讯录接口都有频率限制，千人级突发（集中验证/投票回复/拉群）
# 会触发限流码；这里统一走令牌桶 + 指数退避重试，避免 429 直接丢回复。

_IM_RETRYABLE_CODES = {99991400, 99991402, 91403, 91499}
_IM_MAX_RETRIES = 3
_IM_BASE_DELAY = 0.5

_IM_RATE_LOCK = threading.Lock()
_IM_RATE_STATE = {"tokens": 20.0, "ts": time.monotonic()}


def _im_qps() -> float:
    try:
        return max(1.0, float(os.environ.get("FEISHU_IM_QPS", "20")))
    except ValueError:
        return 20.0


def _im_try_acquire() -> float:
    """尝试取一个令牌：返回 0 表示已取到，否则返回建议等待秒数。"""
    qps = _im_qps()
    with _IM_RATE_LOCK:
        now = time.monotonic()
        _IM_RATE_STATE["tokens"] = min(qps, _IM_RATE_STATE["tokens"]
                                       + (now - _IM_RATE_STATE["ts"]) * qps)
        _IM_RATE_STATE["ts"] = now
        if _IM_RATE_STATE["tokens"] >= 1.0:
            _IM_RATE_STATE["tokens"] -= 1.0
            return 0.0
        return (1.0 - _IM_RATE_STATE["tokens"]) / qps


def _im_acquire_slot() -> None:
    while True:
        wait = _im_try_acquire()
        if wait <= 0:
            return
        time.sleep(wait)


async def _im_acquire_slot_async() -> None:
    while True:
        wait = _im_try_acquire()
        if wait <= 0:
            return
        await asyncio.sleep(wait)


def _im_retryable(code) -> bool:
    try:
        return int(code) in _IM_RETRYABLE_CODES
    except (TypeError, ValueError):
        return False


def _im_call_sync(call):
    """同步调用（运行在线程池里）：取限流令牌 + 限流码指数退避重试，返回 resp。"""
    _im_acquire_slot()
    resp = None
    for attempt in range(_IM_MAX_RETRIES + 1):
        resp = call()
        code = getattr(resp, "code", None)
        if code == 0 or code is None or not _im_retryable(code) or attempt >= _IM_MAX_RETRIES:
            return resp
        time.sleep(_IM_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.2))
    return resp


async def _im_call(call):
    """async 包装：限流 + 重试，SDK 同步调用经 to_thread 移出事件循环。"""
    await _im_acquire_slot_async()
    resp = None
    for attempt in range(_IM_MAX_RETRIES + 1):
        resp = await asyncio.to_thread(call)
        code = getattr(resp, "code", None)
        if code == 0 or code is None or not _im_retryable(code) or attempt >= _IM_MAX_RETRIES:
            return resp
        await asyncio.sleep(_IM_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.2))
    return resp


async def send_text(open_id: str, text: str) -> None:
    """私聊发送文本消息。"""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    req = (CreateMessageRequest.builder()
           .receive_id_type("open_id")
           .request_body(CreateMessageRequestBody.builder()
                         .receive_id(open_id).msg_type("text")
                         .content(json.dumps({"text": text}, ensure_ascii=False))
                         .build())
           .build())

    resp = await _im_call(lambda: client().im.v1.message.create(req))
    if not resp.success():
        raise RuntimeError(f"发消息失败: {resp.code} {resp.msg}")


async def send_card(open_id: str, card: dict) -> str | None:
    """私聊发送交互卡片，返回 message_id。"""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    req = (CreateMessageRequest.builder()
           .receive_id_type("open_id")
           .request_body(CreateMessageRequestBody.builder()
                         .receive_id(open_id).msg_type("interactive")
                         .content(json.dumps(card, ensure_ascii=False))
                         .build())
           .build())

    resp = await _im_call(lambda: client().im.v1.message.create(req))
    if not resp.success():
        raise RuntimeError(f"发卡片失败: {resp.code} {resp.msg}")
    return resp.data.message_id


async def edit_card(message_id: str, card: dict) -> None:
    """编辑已发出的卡片消息内容（im/v1/messages PATCH，用于验证卡片超时过期）。"""
    from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

    req = (PatchMessageRequest.builder()
           .message_id(message_id)
           .request_body(PatchMessageRequestBody.builder()
                         .content(json.dumps(card, ensure_ascii=False))
                         .build())
           .build())
    resp = await _im_call(lambda: client().im.v1.message.patch(req))
    if not resp.success():
        raise RuntimeError(f"编辑卡片失败: {resp.code} {resp.msg}")


async def update_card(token: str, card: dict) -> None:
    """通过卡片回调 token 原地更新卡片（回调后 30 分钟内有效，最多更新 2 次）。"""
    import urllib.request

    def _do():
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/interactive/v1/card/update",
            data=json.dumps({"token": token, "card": card}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {_tenant_token()}"})
        return json.load(urllib.request.urlopen(req, timeout=10))

    await _im_acquire_slot_async()
    resp = None
    for attempt in range(_IM_MAX_RETRIES + 1):
        resp = await asyncio.to_thread(_do)
        code = resp.get("code")
        if code in (0, None) or not _im_retryable(code) or attempt >= _IM_MAX_RETRIES:
            break
        await asyncio.sleep(_IM_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.2))
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"更新卡片失败: {resp.get('code')} {resp.get('msg')}")


_TENANT_TOKEN_CACHE: tuple[str, float] = ("", 0.0)


def _tenant_token() -> str:
    """bot tenant_access_token（带 5 分钟缓存）。"""
    import time
    import urllib.request

    global _TENANT_TOKEN_CACHE
    tok, ts = _TENANT_TOKEN_CACHE
    if tok and time.time() - ts < 300:
        return tok
    from .config import CFG

    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": CFG.app_id, "app_secret": CFG.app_secret}).encode(),
        headers={"Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=10))
    if d.get("code") != 0:
        raise RuntimeError(f"获取 tenant_token 失败: {d.get('msg')}")
    _TENANT_TOKEN_CACHE = (d["tenant_access_token"], time.time())
    return _TENANT_TOKEN_CACHE[0]


def get_user_phone(open_id: str) -> str | None:
    """通过通讯录 API 读取用户手机号（需要开通获取手机号权限）。"""
    from lark_oapi.api.contact.v3 import GetUserRequest

    req = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
    resp = _im_call_sync(lambda: client().contact.v3.user.get(req))
    if not resp.success():
        raise RuntimeError(f"读取用户信息失败: {resp.code} {resp.msg}（可能缺手机号权限或不在应用可见范围）")
    return getattr(resp.data.user, "mobile", None)


def resolve_open_ids_by_phones(phones: list[str]) -> dict[str, str]:
    """手机号 -> open_id，批量（contact:user.base:readonly）。"""
    from lark_oapi.api.contact.v3 import BatchGetIdUserRequest

    out: dict[str, str] = {}
    ps = [p for p in phones if p]
    for i in range(0, len(ps), 100):
        req = (BatchGetIdUserRequest.builder()
               .user_id_type("open_id")
               .request_body({"mobiles": ps[i:i + 100]})
               .build())
        resp = _im_call_sync(lambda: client().contact.v3.user.batch_get_id(req))
        if not resp.success():
            raise RuntimeError(f"手机号查询失败: {resp.code} {resp.msg}")
        for u in (resp.data.user_list or []):
            mobile = getattr(u, "mobile", None) or ""
            uid = getattr(u, "user_id", None)
            if mobile and uid:
                out[mobile.lstrip("+")] = uid
    return out


def list_member_ids(chat_id: str) -> set[str]:
    """拉取群全部成员 open_id（自动翻页）。"""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import GetChatMembersRequest

    out: set[str] = set()
    page_token = ""
    while True:
        b = (GetChatMembersRequest.builder()
             .chat_id(chat_id).member_id_type("open_id").page_size(100))
        if page_token:
            b = b.page_token(page_token)
        resp = _im_call_sync(lambda: client().im.v1.chat_members.get(b.build()))
        if not resp.success():
            raise RuntimeError(f"读取群成员失败: {resp.code} {resp.msg}")
        for m in (resp.data.items or []):
            if m.member_id:
                out.add(m.member_id)
        if not resp.data.has_more:
            break
        page_token = resp.data.page_token or ""
    return out


def update_group_restrictions(chat_id: str,
                              settings: Mapping[str, str] | None = None) -> None:
    """设置群限制；未传入的字段使用默认模板。

    部分租户不开放隐藏群人数能力（232078）。默认值本来就是
    ``all_members``，因此该字段不可用时省略它并继续应用其他限制。
    置顶管理权限目前没有可靠的官方服务端写入接口，不在自动设置范围内。
    """
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (UpdateChatModerationRequest,
                                     UpdateChatModerationRequestBody,
                                     UpdateChatRequest, UpdateChatRequestBody)

    merged = dict(DEFAULT_GROUP_RESTRICTIONS)
    if settings:
        merged.update(settings)
    allowed = set(_CHAT_RESTRICTION_FIELDS) | {"moderation_setting"}
    unknown = set(merged) - allowed
    if unknown:
        raise ValueError(f"不支持的群限制字段: {', '.join(sorted(unknown))}")

    def update_chat_fields(fields: tuple[str, ...]):
        body = UpdateChatRequestBody.builder()
        for field in fields:
            body = getattr(body, field)(merged[field])
        return _im_call_sync(lambda: client().im.v1.chat.update(
            UpdateChatRequest.builder()
            .chat_id(chat_id)
            .user_id_type("open_id")
            .request_body(body.build())
            .build()))

    resp = update_chat_fields(_CHAT_RESTRICTION_FIELDS)
    if (not resp.success() and str(resp.code) == "232078"
            and merged["hide_member_count_setting"] == "all_members"):
        # This setting is optional for the chosen profile and unavailable in
        # tenants without the corresponding product capability.
        resp = update_chat_fields(tuple(
            field for field in _CHAT_RESTRICTION_FIELDS
            if field != "hide_member_count_setting"))
    if not resp.success():
        raise RuntimeError(f"更新群限制失败: chat_id={chat_id} {resp.code} {resp.msg}")

    moderation_body = (UpdateChatModerationRequestBody.builder()
                       .moderation_setting(merged["moderation_setting"])
                       .build())
    moderation_resp = _im_call_sync(lambda: client().im.v1.chat_moderation.update(
        UpdateChatModerationRequest.builder()
        .chat_id(chat_id)
        .user_id_type("open_id")
        .request_body(moderation_body)
        .build()))
    if not moderation_resp.success():
        raise RuntimeError(
            "群基础限制已更新，但发言权限设置失败: "
            f"chat_id={chat_id} {moderation_resp.code} {moderation_resp.msg}")


def create_group(name: str, owner_open_id: str, member_open_ids: list[str],
                 description: str = "", external: bool = True) -> str:
    """机器人建群，返回 chat_id。

    - external=True 建外部群（可拉外部成员），此时必须指定用户当群主（机器人不能当外部群群主）；
    - external=False 建内部群，机器人自己当群主（拉人不受「仅管理员」限制，但不能拉外部成员）。
    - 建群完成后自动应用 DEFAULT_GROUP_RESTRICTIONS。
    """
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateChatRequest, CreateChatRequestBody

    body = (CreateChatRequestBody.builder()
            .name(name)
            .description(description)
            .external(external))
    if external:
        body = body.owner_id(owner_open_id)
    # 指定群主后飞书会自动将其加入群聊；不要再把同一 ID 放进
    # user_id_list，否则部分租户会以 232001 拒绝重复的初始成员列表。
    members = list(dict.fromkeys(
        open_id for open_id in member_open_ids
        if open_id and (not external or open_id != owner_open_id)
    ))
    if members:
        body = body.user_id_list(members)
    request = CreateChatRequest.builder().user_id_type("open_id")
    if external:
        # External groups require a human owner; make the creating bot an admin
        # so it can invite members after creation.
        request = request.set_bot_manager(True)
    resp = _im_call_sync(lambda: client().im.v1.chat.create(
        request.request_body(body.build()).build()))
    if not resp.success():
        raise RuntimeError(f"建群失败: {resp.code} {resp.msg}")
    chat_id = resp.data.chat_id
    try:
        update_group_restrictions(chat_id)
    except Exception as exc:
        raise RuntimeError(f"建群成功但自动设置群限制失败: chat_id={chat_id}；{exc}") from exc
    return chat_id


def disband_group(chat_id: str) -> None:
    """机器人解散群（机器人须是群主）。"""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import DeleteChatRequest

    resp = _im_call_sync(lambda: client().im.v1.chat.delete(
        DeleteChatRequest.builder().chat_id(chat_id).build()))
    if not resp.success():
        raise RuntimeError(f"解散群失败: {resp.code} {resp.msg}")


def add_members(chat_id: str, open_ids: list[str]) -> tuple[int, str | None]:
    """把 open_id 列表分批拉入群，返回 (成功数, 错误信息)。"""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateChatMembersRequest, CreateChatMembersRequestBody

    # 飞书“将用户或机器人拉入群聊”接口单次最多接收 50 个用户。
    # 补拉可能一次命中全部选手，必须在客户端去重并分批提交。
    unique_ids = list(dict.fromkeys(open_id for open_id in open_ids if open_id))
    ok_total = 0
    errors: list[str] = []
    for offset in range(0, len(unique_ids), 50):
        chunk = unique_ids[offset:offset + 50]
        req = (CreateChatMembersRequest.builder()
               .chat_id(chat_id)
               .member_id_type("open_id")
               .request_body(CreateChatMembersRequestBody.builder().id_list(chunk).build())
               .build())
        resp = _im_call_sync(lambda req=req: client().im.v1.chat_members.create(req))
        if not resp.success():
            errors.append(f"第 {offset // 50 + 1} 批: {resp.code} {resp.msg}")
            continue
        failed = [r.fail for r in (resp.data.invalid_id_list or []) if r and r.fail]
        ok_total += len(chunk) - len(failed)
        if failed:
            errors.append(f"第 {offset // 50 + 1} 批 {len(failed)} 人失败: {failed[:3]}")
    return ok_total, ("；".join(errors) if errors else None)
