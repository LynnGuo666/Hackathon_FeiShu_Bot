"""飞书开放平台客户端封装（消息、联系人等，bot 身份，走 lark-oapi SDK）。"""
from __future__ import annotations

import json

from .config import CFG

_CLIENT = None


def client():
    global _CLIENT
    if _CLIENT is None:
        import lark_oapi as lark
        if not CFG.has_app_credentials:
            raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请先配置 .env")
        _CLIENT = lark.Client.builder().app_id(CFG.app_id).app_secret(CFG.app_secret).build()
    return _CLIENT


def send_text(open_id: str, text: str) -> None:
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
    resp = client().im.v1.message.create(req)
    if not resp.success():
        raise RuntimeError(f"发消息失败: {resp.code} {resp.msg}")


def send_card(open_id: str, card: dict) -> str | None:
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
    resp = client().im.v1.message.create(req)
    if not resp.success():
        raise RuntimeError(f"发卡片失败: {resp.code} {resp.msg}")
    return resp.data.message_id


def get_user_phone(open_id: str) -> str | None:
    """通过通讯录 API 读取用户手机号（需要开通获取手机号权限）。"""
    from lark_oapi.api.contact.v3 import GetUserRequest

    req = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
    resp = client().contact.v3.user.get(req)
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
        resp = client().contact.v3.user.batch_get_id(req)
        if not resp.success():
            raise RuntimeError(f"手机号查询失败: {resp.code} {resp.msg}")
        for u in (resp.data.user_list or []):
            mobile = getattr(u, "mobile", None) or ""
            uid = getattr(u, "user_id", None)
            if mobile and uid:
                out[mobile.lstrip("+")] = uid
    return out


def add_members(chat_id: str, open_ids: list[str]) -> tuple[int, str | None]:
    """把 open_id 列表拉入群，返回 (成功数, 错误信息)。"""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateChatMembersRequest, CreateChatMembersRequestBody

    req = (CreateChatMembersRequest.builder()
           .chat_id(chat_id)
           .member_id_type("open_id")
           .request_body(CreateChatMembersRequestBody.builder().id_list(open_ids).build())
           .build())
    resp = client().im.v1.chat_members.create(req)
    if not resp.success():
        return 0, f"{resp.code} {resp.msg}"
    failed = [r.fail for r in (resp.data.invalid_list or []) if r and r.fail]
    ok = len(open_ids) - len(failed)
    return ok, (f"{len(failed)} 人失败: {failed[:3]}" if failed else None)
