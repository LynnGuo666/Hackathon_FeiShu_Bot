#!/usr/bin/env python3
"""给选手数据库 Base 的所有表字段写入描述注释（幂等，可重复执行）。

用法：.venv/bin/python scripts/describe_fields.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.core.base_store import _sdk_client  # noqa: E402
from bot.core.config import CFG  # noqa: E402
from lark_oapi.api.bitable.v1 import (AppTableField,  # noqa: E402
                                      ListAppTableRequest,
                                      ListAppTableFieldRequest,
                                      UpdateAppTableFieldRequest)

# 每张表每个字段的说明（表名 -> {字段名: 描述}）
DESCRIPTIONS: dict[str, dict[str, str]] = {
    "选手表": {
        "选手ID": "唯一标识，格式 WY01-XXXX，同步时按手机号去重后递增分配",
        "姓名": "来自报名表（主报名人优先）",
        "手机号": "去重键，归一化后 11 位大陆手机号",
        "vx号": "报名时登记的微信号，外部用户验证的第二因子（可填后 4 位）",
        "学校": "来自报名表",
        "专业": "来自报名表",
        "年级": "来自报名表（单选）",
        "身份": "来自报名表（单选）",
        "意向角色": "来自报名表（多选）",
        "审核状态": "报名审核状态：审核通过/未审核/审核不通过；任一队伍通过即通过。只决定是否拉群，不影响验证绑定",
        "报名记录ID": "关联报名表原始记录（调试追溯用）",
        "角色": "在队伍中的角色（主报名人/队友）",
        "飞书open_id": "验证成功后绑定的飞书用户 open_id，机器人据此识别用户",
        "验证状态": "是否已完成身份验证：未验证/已验证。验证=绑定身份，与审核状态解耦",
        "入群状态": "已废弃：是否在群以飞书群成员实时数据为准，机器人不再读写此字段",
        "更新时间": "记录最后修改时间（自动）",
        "发言数": "群发言累计条数，机器人自动统计（活跃度模块）",
    },
    "队伍表": {
        "队伍ID": "格式 T-XXXXXXXX（取报名记录 ID 后 8 位）",
        "队长": "关联选手表（主报名人）",
        "队友": "关联选手表（队友 1-4）",
        "是否预组队": "仅「是」的报名记录进本表",
        "同意统一分配": "是否同意官方统一分配队伍",
        "报名记录ID": "关联报名表原始记录，upsert 键",
    },
    "群配置表": {
        "群名": "群显示名（写流水用）",
        "chat_id": "群会话 ID（oc_ 开头），机器人拉人目标",
        "启用": "勾选后验证通过/补拉的选手才会被拉入该群",
        "备注": "自由备注",
    },
    "拉群记录表": {
        "选手": "关联选手表",
        "chat_id": "拉入的目标群",
        "群名": "拉入的目标群名",
        "结果": "成功/失败（单选）",
        "失败原因": "失败时的错误信息",
        "时间": "创建时间（自动）",
    },
    "积分表": {
        "选手": "关联选手表",
        "变动分值": "本次加减的分值",
        "变动后积分": "变动后的余额（可选）",
        "事由": "加分/扣分原因",
        "时间": "创建时间（自动）",
    },
    "项目表": {
        "项目ID": "项目编号，投票列表按此排序",
        "项目名称": "项目名",
        "项目介绍": "项目简介",
        "队伍": "关联队伍表",
        "成员": "关联选手表",
        "仓库链接": "代码仓库 URL",
        "演示链接": "演示/部署 URL",
        "票数": "决赛得票数，机器人投票时 +1",
    },
    "投票表": {
        "投票人": "关联选手表（一人一票由机器人校验）",
        "项目": "关联项目表",
        "时间": "创建时间（自动）",
    },
    "活跃度表": {
        "选手": "关联选手表",
        "日期": "统计日期（YYYY-MM-DD），一人一天一条",
        "发言数": "当天发言条数（机器人每分钟批量落库）",
    },
}


def main() -> None:
    client = _sdk_client()
    resp = client.bitable.v1.app_table.list(
        ListAppTableRequest.builder().app_token(CFG.db_base_token).page_size(100).build())
    if not resp.success():
        raise SystemExit(f"列表面失败: {resp.code} {resp.msg}")

    total, skipped = 0, []
    for t in (resp.data.items or []):
        descs = DESCRIPTIONS.get(t.name)
        if not descs:
            skipped.append(t.name)
            continue
        fr = client.bitable.v1.app_table_field.list(
            ListAppTableFieldRequest.builder().app_token(CFG.db_base_token).table_id(t.table_id).build())
        if not fr.success():
            raise SystemExit(f"列表字段失败 {t.name}: {fr.code} {fr.msg}")
        for f in (fr.data.items or []):
            desc = descs.get(f.field_name)
            if not desc:
                skipped.append(f"{t.name}.{f.field_name}")
                continue
            # update 需要带全 field_name/type；description 只更新描述
            body = (AppTableField.builder()
                    .field_name(f.field_name).type(f.type)
                    .description({"text": desc, "disable_sync": False}).build())
            ur = client.bitable.v1.app_table_field.update(
                UpdateAppTableFieldRequest.builder()
                .app_token(CFG.db_base_token).table_id(t.table_id).field_id(f.field_id)
                .request_body(body).build())
            if not ur.success():
                print(f"  ✗ {t.name}.{f.field_name}: {ur.code} {ur.msg}")
            else:
                total += 1
    print(f"已写入 {total} 个字段描述")
    if skipped:
        print("未覆盖（表/字段无描述配置）:", ", ".join(skipped))


if __name__ == "__main__":
    main()
