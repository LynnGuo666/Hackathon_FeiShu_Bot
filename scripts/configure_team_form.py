#!/usr/bin/env python3
"""配置组队收集表单：队长必填，队友集中为一个可选文本框。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.core.base_store import _sdk_client  # noqa: E402
from bot.core.config import CFG  # noqa: E402
from lark_oapi.api.bitable.v1 import (  # noqa: E402
    AppTableField,
    AppTableFormPatchedField,
    CreateAppTableFieldRequest,
    ListAppTableFieldRequest,
    ListAppTableFormFieldRequest,
    ListAppTableRequest,
    PatchAppTableFormFieldRequest,
)


TABLE_NAME = "组队收集表"
FORM_ID = "vewVh1kqcA"
CAPTAIN_PHONE = "队长手机号（自动校验）"
TEAM_SIZE = "队伍人数"
TEAMMATE_BLOB = "队友信息（可选）"


def patch_form_field(client, table_id: str, field_id: str, **kwargs) -> None:
    body = AppTableFormPatchedField.builder()
    for key, value in kwargs.items():
        getattr(body, key)(value)
    response = client.bitable.v1.app_table_form_field.patch(
        PatchAppTableFormFieldRequest.builder()
        .app_token(CFG.db_base_token)
        .table_id(table_id)
        .form_id(FORM_ID)
        .field_id(field_id)
        .request_body(body.build())
        .build()
    )
    if not response.success():
        raise RuntimeError(f"表单字段 {field_id} 配置失败: {response.code} {response.msg}")


def main() -> None:
    client = _sdk_client()
    tables_response = client.bitable.v1.app_table.list(
        ListAppTableRequest.builder().app_token(CFG.db_base_token).page_size(100).build()
    )
    if not tables_response.success():
        raise RuntimeError(f"读取数据表失败: {tables_response.code} {tables_response.msg}")
    table = next((t for t in (tables_response.data.items or []) if t.name == TABLE_NAME), None)
    if table is None:
        raise RuntimeError(f"找不到数据表: {TABLE_NAME}")

    fields_response = client.bitable.v1.app_table_field.list(
        ListAppTableFieldRequest.builder()
        .app_token(CFG.db_base_token)
        .table_id(table.table_id)
        .page_size(100)
        .build()
    )
    if not fields_response.success():
        raise RuntimeError(f"读取字段失败: {fields_response.code} {fields_response.msg}")
    fields = {f.field_name: f for f in (fields_response.data.items or [])}

    if CAPTAIN_PHONE not in fields:
        response = client.bitable.v1.app_table_field.create(
            CreateAppTableFieldRequest.builder()
            .app_token(CFG.db_base_token)
            .table_id(table.table_id)
            .request_body(
                AppTableField.builder()
                .field_name(CAPTAIN_PHONE)
                .type(13)
                .ui_type("Phone")
                .build()
            )
            .build()
        )
        if not response.success():
            raise RuntimeError(f"创建手机号字段失败: {response.code} {response.msg}")
        fields_response = client.bitable.v1.app_table_field.list(
            ListAppTableFieldRequest.builder()
            .app_token(CFG.db_base_token)
            .table_id(table.table_id)
            .page_size(100)
            .build()
        )
        fields = {f.field_name: f for f in (fields_response.data.items or [])}

    if TEAMMATE_BLOB not in fields:
        response = client.bitable.v1.app_table_field.create(
            CreateAppTableFieldRequest.builder()
            .app_token(CFG.db_base_token)
            .table_id(table.table_id)
            .request_body(
                AppTableField.builder().field_name(TEAMMATE_BLOB).type(1).ui_type("Text").build()
            )
            .build()
        )
        if not response.success():
            raise RuntimeError(f"创建队友字段失败: {response.code} {response.msg}")
        fields_response = client.bitable.v1.app_table_field.list(
            ListAppTableFieldRequest.builder()
            .app_token(CFG.db_base_token)
            .table_id(table.table_id)
            .page_size(100)
            .build()
        )
        fields = {f.field_name: f for f in (fields_response.data.items or [])}

    form_fields_response = client.bitable.v1.app_table_form_field.list(
        ListAppTableFormFieldRequest.builder()
        .app_token(CFG.db_base_token)
        .table_id(table.table_id)
        .form_id(FORM_ID)
        .page_size(100)
        .build()
    )
    if not form_fields_response.success():
        raise RuntimeError(f"读取表单字段失败: {form_fields_response.code} {form_fields_response.msg}")
    form_fields = {
        fields_by_id.field_name: form_field
        for form_field in (form_fields_response.data.items or [])
        for fields_by_id in fields.values()
        if fields_by_id.field_id == form_field.field_id
    }

    # 旧的 4 组 × 3 项字段不再直接展示，避免首次打开表单出现一长串空问题。
    hidden_names = [
        *(f"队友{i}{part}" for i in range(1, 5) for part in ("姓名", "手机号", "邮箱")),
        "队长手机号",
        "项目名称",
        "备注",
        "导入状态",
        "导入结果",
    ]
    for name in hidden_names:
        field = form_fields.get(name)
        if field:
            patch_form_field(client, table.table_id, field.field_id, visible=False)

    for name in ("队长姓名", "队长邮箱"):
        field = form_fields.get(name)
        if field:
            patch_form_field(client, table.table_id, field.field_id, visible=True, required=True)

    phone_form_field = form_fields.get(CAPTAIN_PHONE)
    team_size_form_field = form_fields.get(TEAM_SIZE)
    teammate_form_field = form_fields.get(TEAMMATE_BLOB)
    if not phone_form_field or not team_size_form_field or not teammate_form_field:
        raise RuntimeError("新字段尚未出现在表单字段列表中")
    patch_form_field(
        client,
        table.table_id,
        phone_form_field.field_id,
        title="队长手机号",
        description="请输入大陆11位手机号，提交时会自动校验格式。",
        visible=True,
        required=True,
    )
    patch_form_field(
        client,
        table.table_id,
        team_size_form_field.field_id,
        title=TEAM_SIZE,
        description="单选 1–4，表示队长之外的队友人数；队伍总人数必须为 3–5 人。",
        visible=True,
        required=True,
    )
    patch_form_field(
        client,
        table.table_id,
        teammate_form_field.field_id,
        title=TEAMMATE_BLOB,
        description=(
            "按“队伍人数”逐行填写，每行一名队友，格式：姓名 | 手机号 | 邮箱；"
            "最多4行。条件显示需在飞书表单编辑界面另行配置。"
        ),
        visible=True,
        required=False,
    )
    print("team form configured", table.table_id, FORM_ID)


if __name__ == "__main__":
    main()
