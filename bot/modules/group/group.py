"""拉群模块：读取群配置表，按身份把用户拉入匹配的群，写拉群记录；支持补拉。

设计：
- 不维护「入群状态」字段，是否已在群里以飞书群成员实时数据为准——每次拉群前先查群成员列表，只拉缺失的人。
- 按身份分流：群配置表「面向身份」多选（选手/组委会-导师/组委会-主办方/全部，留空视为选手），
  用户带 identities 列表，群面向身份与用户身份有交集才拉（含「全部」则任何身份可入）。
- 新增群后无需任何操作，定时自动补拉会把所有已验证用户补进匹配的新群。
"""
from __future__ import annotations

import logging

from ...core.base_store import BaseStore
from ...core.card_kit import result_card
from ...core.config import CFG
from ...core.lark_client import add_members, list_member_ids, send_card, send_text
from ...core.registry import REGISTRY, MsgCtx
from ..sync import is_admin
from ..sync.sync import audit_status

log = logging.getLogger(__name__)

# 身份常量
ID_CONTESTANT = "选手"
ID_ALL = "全部"


def _text(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, list):
        return "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in cell).strip()
    return str(cell).strip()


def _multi(cell) -> list[str]:
    """多选字段读回 -> 选项字符串列表。"""
    if isinstance(cell, list):
        return [_text(x) for x in cell if _text(x)]
    s = _text(cell)
    return [s] if s else []


def enabled_chats(store: BaseStore) -> list[dict]:
    """所有启用群，带面向身份列表（留空视为选手）。"""
    out = []
    for r in store.list_records(CFG.tbl_group_config):
        f = r.get("fields") or {}
        if f.get("启用") is True and f.get("chat_id"):
            audiences = _multi(f.get("面向身份")) or [ID_CONTESTANT]
            out.append({"chat_id": str(f["chat_id"]).strip(), "群名": str(f.get("群名") or ""),
                        "面向身份": audiences})
    return out


def _chat_matches(chat: dict, identities: list[str]) -> bool:
    """群面向身份与用户身份是否有交集（群含「全部」则任何身份可入）。"""
    aud = chat["面向身份"]
    if ID_ALL in aud:
        return True
    return bool(set(aud) & set(identities))


def verified_users(store: BaseStore) -> list[dict]:
    """所有已验证用户（选手 + 组委会），带身份列表。

    选手身份 = 选手；组委会身份 = 组委会-{身份}；双重身份两者都有。
    """
    out = []
    for r in store.list_records(CFG.tbl_contestants):
        f = r.get("fields") or {}
        if str(f.get("验证状态") or "") == "已验证" and f.get("飞书open_id"):
            out.append({"open_id": str(f["飞书open_id"]), "选手ID": str(f.get("选手ID") or ""),
                        "record_id": r["record_id"], "identities": [ID_CONTESTANT]})
    for r in store.list_records(CFG.tbl_organizers):
        f = r.get("fields") or {}
        if str(f.get("验证状态") or "") == "已验证" and f.get("飞书open_id"):
            oid = str(f["飞书open_id"])
            ident = f"组委会-{_text(f.get('身份')) or '主办方'}"
            exist = next((u for u in out if u["open_id"] == oid), None)
            if exist:
                exist["identities"].append(ident)  # 双重身份
            else:
                out.append({"open_id": oid, "选手ID": str(f.get("姓名") or ""),
                            "record_id": r["record_id"], "identities": [ident]})
    return out


def pull_user_into_groups(users: list[dict]) -> tuple[int, str | None]:
    """把若干用户拉入与其身份匹配的所有启用群（只拉群里还没有的人），写拉群记录。

    users: [{open_id, 选手ID, record_id, identities}]；返回 (成功拉入人次, 失败信息)。
    """
    store = BaseStore(CFG.db_base_token)
    chats = enabled_chats(store)
    ok_total, err = 0, None
    log_rows = []
    for chat in chats:
        targets = [u for u in users if _chat_matches(chat, u.get("identities") or [ID_CONTESTANT])]
        if not targets:
            continue
        try:
            member_ids = list_member_ids(chat["chat_id"])
        except Exception as e:
            err = f"{chat['群名']}: {e}"
            log.warning("读取群成员失败，跳过该群: %s", err)
            continue
        missing = [u["open_id"] for u in targets if u["open_id"] not in member_ids]
        if not missing:
            continue
        ok, fail = add_members(chat["chat_id"], missing)
        ok_total += ok
        if fail:
            err = f"{chat['群名']}: {fail}"
        for u in targets:
            if u["open_id"] in member_ids:
                continue  # 已在群里的人不写流水
            log_rows.append({"fields": {
                "选手": [{"id": u["record_id"]}] if u.get("record_id") else [],
                "chat_id": chat["chat_id"],
                "群名": chat["群名"],
                "结果": "成功" if not fail else "失败",
                "失败原因": fail or "",
            }})
    if log_rows:
        store.batch_create(CFG.tbl_pull_log, log_rows)
    return ok_total, err


async def handle_backfill(ctx: MsgCtx) -> None:
    if ctx.chat_type == "p2p" and not is_admin(ctx.open_id):
        await send_text(ctx.open_id, "补拉指令仅限管理员使用。")
        return
    store = BaseStore(CFG.db_base_token)
    targets = verified_users(store)
    if not targets:
        await send_text(ctx.open_id, "没有已验证的用户。")
        return
    ok, fail = pull_user_into_groups(targets)
    await send_card(ctx.open_id, result_card("补拉完成", True, [
        f"- 已验证用户：{len(targets)}", f"- 本次补拉入群人次：{ok}",
        *( [f"- 失败：{fail}"] if fail else [] )]))


@REGISTRY.job("自动补拉", CFG.backfill_interval_minutes)
async def auto_backfill_job() -> None:
    """定时把所有已验证用户补进与其身份匹配的缺失群（含新增群），间隔为 0 时禁用。"""
    if CFG.backfill_interval_minutes <= 0:
        return
    ok, fail = pull_user_into_groups(verified_users(BaseStore(CFG.db_base_token)))
    if ok or fail:
        log.info("自动补拉: 成功 %d 人次%s", ok, f"，失败: {fail}" if fail else "")


async def promote_approved() -> int:
    """审核通过补拉：扫描「已验证但审核状态≠审核通过」的选手，
    审核状态后来变为「审核通过」的自动拉入匹配的群。返回处理人数。

    背景：验证 = 绑定身份，与审核解耦；未审核通过的用户验证成功后先不入群，
    等报名审核通过后由本任务自动补拉（无需重新验证）。组委会成员无审核概念，由自动补拉覆盖。
    """
    store = BaseStore(CFG.db_base_token)
    targets = []
    for r in store.list_records(CFG.tbl_contestants):
        f = r.get("fields") or {}
        if str(f.get("验证状态") or "") != "已验证" or not f.get("飞书open_id"):
            continue
        if audit_status(f) == "审核通过":
            targets.append({"open_id": str(f["飞书open_id"]), "选手ID": str(f.get("选手ID") or ""),
                            "record_id": r["record_id"], "identities": [ID_CONTESTANT]})
    if not targets:
        return 0
    ok, fail = pull_user_into_groups(targets)
    if ok or fail:
        log.info("审核通过补拉: %d 名选手，成功 %d 人次%s", len(targets), ok, f"，失败: {fail}" if fail else "")
    return len(targets)


@REGISTRY.job("审核通过补拉", CFG.backfill_interval_minutes)
async def promote_approved_job() -> None:
    if CFG.backfill_interval_minutes <= 0:
        return
    await promote_approved()


REGISTRY.command("补拉")(handle_backfill)
