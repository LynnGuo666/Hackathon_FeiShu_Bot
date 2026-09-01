"""同步群配置表中的面向身份选项（飞书字段 schema 级操作，飞书专属能力）。

不进通用 Repository 接口：未来 API 后端若提供等价能力，再单独抽象。
"""
from __future__ import annotations

import asyncio
import logging

from ...core.config import CFG

log = logging.getLogger(__name__)


def _sync_options_sync() -> None:
    from ...core.base_store import _sdk_client
    from lark_oapi.api.bitable.v1 import (
        AppTableField,
        AppTableFieldProperty,
        ListAppTableFieldRequest,
        UpdateAppTableFieldRequest,
    )

    client = _sdk_client()
    organizer_fields = client.bitable.v1.app_table_field.list(
        ListAppTableFieldRequest.builder()
        .app_token(CFG.db_base_token)
        .table_id(_resolve_table_id(CFG.tbl_organizers))
        .build())
    org_options = []
    for field in (organizer_fields.data.items or []):
        if field.field_name == "身份" and field.property:
            org_options = [
                option.name
                for option in (field.property.options or [])
                if getattr(option, "name", None)
            ]

    group_fields = client.bitable.v1.app_table_field.list(
        ListAppTableFieldRequest.builder()
        .app_token(CFG.db_base_token)
        .table_id(_resolve_table_id(CFG.tbl_group_config))
        .build())
    current_options = []
    audience_field_id = None
    for field in (group_fields.data.items or []):
        if field.field_name == "面向身份":
            audience_field_id = field.field_id
            if field.property:
                current_options = [
                    option.name
                    for option in (field.property.options or [])
                    if getattr(option, "name", None)
                ]
    if not audience_field_id:
        log.warning("群配置表无「面向身份」列，跳过选项同步")
        return

    wanted = ["选手"] + [f"组委会-{option}" for option in org_options] + ["全部"]
    missing = [option for option in wanted if option not in current_options]
    if not missing:
        return

    body = (
        AppTableField.builder()
        .field_name("面向身份")
        .type(4)
        .property(AppTableFieldProperty.builder()
                  .options([{"name": option} for option in current_options + missing])
                  .build())
        .build()
    )
    response = client.bitable.v1.app_table_field.update(
        UpdateAppTableFieldRequest.builder()
        .app_token(CFG.db_base_token)
        .table_id(_resolve_table_id(CFG.tbl_group_config))
        .field_id(audience_field_id)
        .request_body(body)
        .build())
    if response.success():
        log.info("群配置表「面向身份」选项已同步，新增: %s", missing)
    else:
        log.warning("同步「面向身份」选项失败: %s %s", response.code, response.msg)


def _resolve_table_id(table: str) -> str:
    from ...core.base_store import BaseStore

    return BaseStore(CFG.db_base_token).resolve_table_id(table)


async def sync_group_audience_options() -> None:
    """把组委会表的身份选项补充到群配置表，操作保持幂等（SDK 调用移出事件循环）。"""
    try:
        await asyncio.to_thread(_sync_options_sync)
    except Exception:
        log.exception("同步群配置表选项出错（不影响主流程）")
