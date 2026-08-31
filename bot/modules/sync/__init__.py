"""同步模块：注册「同步」指令与定时同步任务；管理员识别。"""
from __future__ import annotations

import logging
import threading
import time

from ...core.card_kit import result_card
from ...core.config import CFG
from ...core.lark_client import send_card, send_text
from ...core.registry import REGISTRY, MsgCtx
from .sync import run_sync

log = logging.getLogger(__name__)

# 管理员 = 飞书开发者后台「应用协作者」（owner / administrator 等），
# 启动时拉取并每 10 分钟刷新；.env ADMIN_OPEN_IDS 作为兜底（API 失败时仍可用）。
_admins: set[str] = set(CFG.admin_open_ids)
_admins_lock = threading.Lock()
_collaborators_failed = False  # 协作者 API 从未成功过时置 True，提示用户兜底配置


def _fetch_collaborator_admins() -> set[str]:
    """调应用协作者 API，返回协作者 open_id 集合（type 为 owner/administrator 等，全部算管理员）。"""
    from lark_oapi.api.application.v6 import GetApplicationCollaboratorsRequest

    from ...core.lark_client import client
    resp = client().application.v6.application_collaborators.get(
        GetApplicationCollaboratorsRequest.builder().app_id(CFG.app_id).user_id_type("open_id").build())
    if not resp.success():
        raise RuntimeError(f"协作者 API 失败: {resp.code} {resp.msg}")
    return {c.user_id for c in (resp.data.collaborators or []) if c.user_id}


def test_recipient() -> str:
    """测试消息收件人：优先 .env TEST_OPEN_ID，否则取协作者里的 Lynn（administrator），再否则 owner。"""
    if CFG.test_open_id:
        return CFG.test_open_id
    try:
        from lark_oapi.api.application.v6 import GetApplicationCollaboratorsRequest

        from ...core.lark_client import client
        resp = client().application.v6.application_collaborators.get(
            GetApplicationCollaboratorsRequest.builder().app_id(CFG.app_id).user_id_type("open_id").build())
        if resp.success():
            collabs = [(c.type, c.user_id) for c in (resp.data.collaborators or []) if c.user_id]
            for want in ("administrator", "owner"):
                for t, uid in collabs:
                    if t == want:
                        return uid
            if collabs:
                return collabs[0][1]
    except Exception:
        pass
    return ""


def refresh_admins() -> None:
    """刷新管理员名单（协作者 API + .env 兜底合并）。"""
    global _collaborators_failed
    try:
        ids = _fetch_collaborator_admins()
        with _admins_lock:
            _admins.clear()
            _admins.update(ids)
            _admins.update(CFG.admin_open_ids)
        _collaborators_failed = False
        log.info("管理员名单已刷新: %d 人（应用协作者）", len(ids))
    except Exception as e:
        _collaborators_failed = True
        with _admins_lock:
            _admins.update(CFG.admin_open_ids)
        log.warning("获取应用协作者失败，管理员回落到 .env ADMIN_OPEN_IDS（%d 人）: %s",
                    len(CFG.admin_open_ids), e)


def is_admin(open_id: str) -> bool:
    return open_id in _admins


def admins_status_line() -> str:
    if _collaborators_failed and not CFG.admin_open_ids:
        return "⚠️ 管理员名单为空：协作者 API 不可用且未配置 ADMIN_OPEN_IDS，管理指令暂无人可用。"
    src = "应用协作者" if not _collaborators_failed else ".env 兜底"
    return f"当前管理员 {len(_admins)} 人（来源：{src}）。"


async def handle_sync(ctx: MsgCtx) -> None:
    await send_text(ctx.open_id, "开始同步报名表，请稍候…")
    stats = run_sync()
    await send_card(ctx.open_id, result_card(
        "同步完成", True,
        [f"- {k}：**{v}**" for k, v in stats.items()]))


async def handle_admins(ctx: MsgCtx) -> None:
    """「管理员」：查看当前管理员名单与来源。"""
    await send_card(ctx.open_id, result_card("管理员", True, [admins_status_line()]))


REGISTRY.command("同步")(handle_sync)
REGISTRY.command("管理员")(handle_admins)


@REGISTRY.job("报名表自动同步", CFG.sync_interval_minutes)
async def sync_job() -> None:
    run_sync()
    sync_group_audience_options()


def sync_group_audience_options() -> None:
    """群配置表「面向身份」多选选项随组委会表「身份」选项自动同步：
    组委会表出现新身份时，给群配置表补上「组委会-{身份}」选项（幂等）。"""
    try:
        from ...core.base_store import _sdk_client
        from lark_oapi.api.bitable.v1 import (AppTableField, AppTableFieldProperty,
                                              ListAppTableFieldRequest,
                                              UpdateAppTableFieldRequest)

        client = _sdk_client()
        # 组委会表「身份」列选项（option 对象有 .name 属性）
        fr = client.bitable.v1.app_table_field.list(
            ListAppTableFieldRequest.builder().app_token(CFG.db_base_token)
            .table_id(_resolve_table_id(CFG.tbl_organizers)).build())
        org_options = []
        for f in (fr.data.items or []):
            if f.field_name == "身份" and f.property:
                org_options = [o.name for o in (f.property.options or []) if getattr(o, "name", None)]
        # 群配置表「面向身份」列
        fr2 = client.bitable.v1.app_table_field.list(
            ListAppTableFieldRequest.builder().app_token(CFG.db_base_token)
            .table_id(_resolve_table_id(CFG.tbl_group_config)).build())
        cur_options = []
        audience_fid = None
        for f in (fr2.data.items or []):
            if f.field_name == "面向身份":
                audience_fid = f.field_id
                if f.property:
                    cur_options = [o.name for o in (f.property.options or []) if getattr(o, "name", None)]
        if not audience_fid:
            log.warning("群配置表无「面向身份」列，跳过选项同步")
            return
        want = ["选手"] + [f"组委会-{o}" for o in org_options] + ["全部"]
        missing = [o for o in want if o not in cur_options]
        if not missing:
            return
        new_options = [{"name": o} for o in cur_options + missing]
        body = (AppTableField.builder().field_name("面向身份").type(4)
                .property(AppTableFieldProperty.builder().options(new_options).build()).build())
        ur = client.bitable.v1.app_table_field.update(
            UpdateAppTableFieldRequest.builder().app_token(CFG.db_base_token)
            .table_id(_resolve_table_id(CFG.tbl_group_config)).field_id(audience_fid)
            .request_body(body).build())
        if ur.success():
            log.info("群配置表「面向身份」选项已同步，新增: %s", missing)
        else:
            log.warning("同步「面向身份」选项失败: %s %s", ur.code, ur.msg)
    except Exception:
        log.exception("同步群配置表选项出错（不影响主流程）")


def _resolve_table_id(table: str) -> str:
    from ...core.base_store import BaseStore
    return BaseStore(CFG.db_base_token).resolve_table_id(table)


@REGISTRY.job("管理员名单刷新", 10)
async def admins_refresh_job() -> None:
    refresh_admins()
