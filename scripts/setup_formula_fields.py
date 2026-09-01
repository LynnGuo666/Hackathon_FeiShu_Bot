#!/usr/bin/env python3
"""一次性配置：在选手数据库 Base 添加聚合公式字段（幂等，已存在则跳过）。

聚合字段把「票数 / 积分总分 / 累计发言」的读改写从机器人进程移到飞书服务端：
- 项目表.票数：查找引用投票表（经「项目」反向关联），统计方式 COUNT
- 选手表.积分：查找引用积分表（经「选手」反向关联），对「变动分值」SUM
- 选手表.累计发言：查找引用活跃度表（经「选手」反向关联），对「发言数」SUM

用法：.venv/bin/python scripts/setup_formula_fields.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.core.base_store import _sdk_client  # noqa: E402
from bot.core.config import CFG  # noqa: E402
from lark_oapi.api.bitable.v1 import (AppTableField,  # noqa: E402
                                      ListAppTableRequest,
                                      ListAppTableFieldRequest)

# (表名, 字段名, 类型, 属性)
# type 19 = Formula；公式里用 LOOKUP/聚合函数引用关联字段
FORMULA_FIELDS: list[tuple[str, str, dict]] = [
    ("项目表", "票数公式",
     {"formula_expression": "COUNT([投票表.项目])"}),
    ("选手表", "积分公式",
     {"formula_expression": "SUM([积分表.选手.变动分值])"}),
    ("选手表", "累计发言",
     {"formula_expression": "SUM([活跃度表.选手.发言数])"}),
]


def main() -> None:
    client = _sdk_client()
    token = CFG.db_base_token
    resp = client.bitable.v1.app_table.list(
        ListAppTableRequest.builder().app_token(token).page_size(100).build())
    tables = {t.name: t.table_id for t in (resp.data.items or [])}

    for table_name, field_name, props in FORMULA_FIELDS:
        tid = tables.get(table_name)
        if not tid:
            print(f"[跳过] 表不存在: {table_name}")
            continue
        # 幂等：已存在同名字段则跳过
        existing = client.bitable.v1.app_table_field.list(
            ListAppTableFieldRequest.builder()
            .app_token(token).table_id(tid).page_size(100).build())
        if any(f.field_name == field_name for f in (existing.data.items or [])):
            print(f"[跳过] {table_name}.{field_name} 已存在")
            continue
        field = (AppTableField.builder()
                 .field_name(field_name)
                 .type(19)  # Formula
                 .property(props)
                 .build())
        resp2 = client.bitable.v1.app_table_field.create(
            __import__("lark_oapi.api.bitable.v1", fromlist=["CreateAppTableFieldRequest"])
            .CreateAppTableFieldRequest.builder()
            .app_token(token).table_id(tid)
            .request_body(field).build())
        if resp2.success():
            print(f"[完成] {table_name}.{field_name}")
        else:
            print(f"[失败] {table_name}.{field_name}: {resp2.code} {resp2.msg}")
            print("       公式语法如不被当前租户接受，请在 Base 界面手动添加查找引用字段：")
            print(f"       {table_name} <- {props['formula_expression']}")


if __name__ == "__main__":
    main()
