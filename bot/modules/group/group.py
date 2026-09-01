"""拉群模块：读取群配置，按身份把用户拉入匹配的群，写拉群记录；支持补拉。

设计：
- 不维护「入群状态」字段，是否已在群里以飞书群成员实时数据为准——每次拉群前先查群成员列表，只拉缺失的人。
- 按身份分流：群配置「面向身份」多选（选手/组委会-导师/组委会-主办方/全部，留空视为选手），
  用户带 identities 列表，群面向身份与用户身份有交集才拉（含「全部」则任何身份可入）。
- 新增群后无需任何操作，定时自动补拉会把所有已验证用户补进匹配的新群。

存储一律经 SVC 仓库接口（core.service），不直接接触飞书字段。
"""
from __future__ import annotations

import logging

from ...core.lark_client import add_members, list_member_ids
from ...core.models import Contestant, GroupConfig, Organizer, PullLog
from ...core.service import SVC

log = logging.getLogger(__name__)

# 身份常量
ID_CONTESTANT = "选手"
ID_ALL = "全部"


async def enabled_chats() -> list[GroupConfig]:
    """所有启用群（留空面向身份视为选手）。"""
    out = []
    for cfg in await SVC.groups.list_configs():
        if cfg.enabled and cfg.chat_id:
            cfg.audiences = cfg.audiences or [ID_CONTESTANT]
            out.append(cfg)
    return out


def _chat_matches(chat: GroupConfig, identities: list[str]) -> bool:
    """群面向身份与用户身份是否有交集（群含「全部」则任何身份可入）。"""
    aud = chat.audiences
    if ID_ALL in aud:
        return True
    return bool(set(aud) & set(identities))


async def verified_users() -> list[dict]:
    """所有已验证用户（选手 + 组委会），带身份列表。

    选手身份 = 选手；组委会身份 = 组委会-{身份}；双重身份两者都有。
    """
    out: list[dict] = []
    org_by_open_id: dict[str, dict] = {}
    for o in await SVC.organizers.list_all():
        if o.verified and o.open_id:
            org_by_open_id[o.open_id] = {"open_id": o.open_id, "选手ID": o.name,
                                         "record_id": o.record_id,
                                         "identities": [o.committee_identity]}
    for c in await SVC.contestants.list_all():
        if c.verified and c.open_id:
            entry = {"open_id": c.open_id, "选手ID": c.contestant_no,
                     "record_id": c.record_id, "identities": [ID_CONTESTANT]}
            org = org_by_open_id.pop(c.open_id, None)
            if org:
                entry["identities"].append(org["identities"][0])  # 双重身份
            out.append(entry)
    out.extend(org_by_open_id.values())
    return out


async def pull_user_into_groups(users: list[dict]) -> tuple[int, str | None]:
    """把若干用户拉入与其身份匹配的所有启用群（只拉群里还没有的人），写拉群记录。

    users: [{open_id, 选手ID, record_id, identities}]；返回 (成功拉入人次, 失败信息)。
    """
    import asyncio

    chats = await enabled_chats()
    ok_total, err = 0, None
    log_rows: list[PullLog] = []
    for chat in chats:
        targets = [u for u in users if _chat_matches(chat, u.get("identities") or [ID_CONTESTANT])]
        if not targets:
            continue
        try:
            member_ids = await asyncio.to_thread(list_member_ids, chat.chat_id)
        except Exception as e:
            err = f"{chat.name}: {e}"
            log.warning("读取群成员失败，跳过该群: %s", err)
            continue
        missing = [u["open_id"] for u in targets if u["open_id"] not in member_ids]
        if not missing:
            continue
        ok, fail = await asyncio.to_thread(add_members, chat.chat_id, missing)
        ok_total += ok
        if fail:
            err = f"{chat.name}: {fail}"
        for u in targets:
            if u["open_id"] in member_ids:
                continue  # 已在群里的人不写流水
            log_rows.append(PullLog(
                contestant_id=u.get("record_id") or "",
                chat_id=chat.chat_id,
                group_name=chat.name,
                result="失败" if fail else "成功",
                fail_reason=fail or "",
            ))
    if log_rows:
        await SVC.groups.append_pull_log(log_rows)
    return ok_total, err


async def auto_backfill_job() -> None:
    """定时把所有已验证用户补进与其身份匹配的缺失群（含新增群），间隔为 0 时禁用。"""
    from ...core.config import CFG
    if CFG.backfill_interval_minutes <= 0:
        return
    ok, fail = await pull_user_into_groups(await verified_users())
    if ok or fail:
        log.info("自动补拉: 成功 %d 人次%s", ok, f"，失败: {fail}" if fail else "")


async def promote_approved() -> int:
    """审核通过补拉：扫描「已验证但审核状态≠审核通过」的选手，
    审核状态后来变为「审核通过」的自动拉入匹配的群。返回处理人数。

    背景：验证 = 绑定身份，与审核解耦；未审核通过的用户验证成功后先不入群，
    等报名审核通过后由本任务自动补拉（无需重新验证）。组委会成员无审核概念，由自动补拉覆盖。
    """
    targets = []
    for c in await SVC.contestants.list_all():
        if c.verified and c.open_id and c.approved:
            targets.append({"open_id": c.open_id, "选手ID": c.contestant_no,
                            "record_id": c.record_id, "identities": [ID_CONTESTANT]})
    if not targets:
        return 0
    ok, fail = await pull_user_into_groups(targets)
    if ok or fail:
        log.info("审核通过补拉: %d 名选手，成功 %d 人次%s", len(targets), ok, f"，失败: {fail}" if fail else "")
    return len(targets)


async def promote_approved_job() -> None:
    from ...core.config import CFG
    if CFG.backfill_interval_minutes <= 0:
        return
    await promote_approved()
