"""拉群模块：读取群配置表，把已验证选手拉入所有启用群，写拉群记录；支持补拉。

设计：不维护「入群状态」字段，是否已在群里以飞书群成员实时数据为准——
每次拉群前先查群成员列表，只拉缺失的人。新增群后无需任何操作，
定时自动补拉（或手动「补拉」）会把所有已验证选手补进新群。
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


def enabled_chats(store: BaseStore) -> list[dict]:
    out = []
    for r in store.list_records(CFG.tbl_group_config):
        f = r.get("fields") or {}
        if f.get("启用") is True and f.get("chat_id"):
            out.append({"chat_id": str(f["chat_id"]).strip(), "群名": str(f.get("群名") or "")})
    return out


def verified_users(store: BaseStore) -> list[dict]:
    """所有已验证且有 open_id 的选手。"""
    out = []
    for r in store.list_records(CFG.tbl_contestants):
        f = r.get("fields") or {}
        if str(f.get("验证状态") or "") == "已验证" and f.get("飞书open_id"):
            out.append({"open_id": str(f["飞书open_id"]), "选手ID": str(f.get("选手ID") or ""),
                        "record_id": r["record_id"]})
    return out


def pull_user_into_groups(users: list[dict]) -> tuple[int, str | None]:
    """把若干选手拉入所有启用群（只拉群里还没有的人），写拉群记录。

    users: [{open_id, 选手ID, record_id}]；返回 (成功拉入人次, 失败信息)。
    """
    store = BaseStore(CFG.db_base_token)
    chats = enabled_chats(store)
    ok_total, err = 0, None
    log_rows = []
    for chat in chats:
        try:
            member_ids = list_member_ids(chat["chat_id"])
        except Exception as e:
            err = f"{chat['群名']}: {e}"
            log.warning("读取群成员失败，跳过该群: %s", err)
            continue
        missing = [u["open_id"] for u in users if u["open_id"] not in member_ids]
        if not missing:
            continue
        ok, fail = add_members(chat["chat_id"], missing)
        ok_total += ok
        if fail:
            err = f"{chat['群名']}: {fail}"
        for u in users:
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
        await send_text(ctx.open_id, "没有已验证的选手。")
        return
    ok, fail = pull_user_into_groups(targets)
    await send_card(ctx.open_id, result_card("补拉完成", True, [
        f"- 已验证选手：{len(targets)}", f"- 本次补拉入群人次：{ok}",
        *( [f"- 失败：{fail}"] if fail else [] )]))


@REGISTRY.job("自动补拉", CFG.backfill_interval_minutes)
async def auto_backfill_job() -> None:
    """定时把所有已验证选手补进缺失的群（含新增群），间隔为 0 时禁用。"""
    if CFG.backfill_interval_minutes <= 0:
        return
    ok, fail = pull_user_into_groups(verified_users(BaseStore(CFG.db_base_token)))
    if ok or fail:
        log.info("自动补拉: 成功 %d 人次%s", ok, f"，失败: {fail}" if fail else "")


async def promote_approved() -> int:
    """审核通过补拉：扫描「已验证但审核状态≠审核通过」的选手，
    审核状态后来变为「审核通过」的自动拉入所有启用群。返回处理人数。

    背景：验证 = 绑定身份，与审核解耦；未审核通过的用户验证成功后先不入群，
    等报名审核通过后由本任务自动补拉（无需重新验证）。
    """
    store = BaseStore(CFG.db_base_token)
    targets = []
    for r in store.list_records(CFG.tbl_contestants):
        f = r.get("fields") or {}
        if str(f.get("验证状态") or "") != "已验证" or not f.get("飞书open_id"):
            continue
        if audit_status(f) == "审核通过":
            targets.append({"open_id": str(f["飞书open_id"]), "选手ID": str(f.get("选手ID") or ""),
                            "record_id": r["record_id"]})
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
