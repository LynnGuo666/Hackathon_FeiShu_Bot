"""飞书多维表格字段映射层：业务英文键 -> 表格中文字段名（写路径唯一出处）。

业务模块与仓库层一律用英文业务键读写实体字段（open_id / verify_status / ...）；
读路径的中文->英文转换在 repos.py 的 _to_* 实体映射里（field_names 常量），
写路径的英文->中文翻译与 link 字段值整形只存在于此处。

历史 bug 教训：写路径曾直接把英文键透传给 batch_update，飞书对未知字段静默忽略，
导致「验证绑定」「同步更新」等写入全部失效。所有写操作必须经 encode() 翻译，
未登记的业务键直接抛错（宁可失败也不静默丢写）。
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from . import field_names as F


@dataclass(frozen=True)
class TableMap:
    """一张表的写路径映射：业务键 -> 中文字段名；links 中的键值需整形为 [{"id":...}]。"""
    fields: dict[str, str]
    links: frozenset[str] = frozenset()


CONTESTANT = TableMap(fields={
    "contestant_no": F.C_NO,
    "name": F.C_NAME,
    "phone": F.C_PHONE,
    "email": F.C_EMAIL,
    "vx": F.C_VX,
    "school": F.C_SCHOOL,
    "major": F.C_MAJOR,
    "grade": F.C_GRADE,
    "identity": F.C_IDENTITY,
    "intent_roles": F.C_INTENT,
    "audit_status": F.C_AUDIT,
    "reg_record_id": F.C_REG_RECORD,
    "open_id": F.C_OPEN_ID,
    "verify_status": F.C_VERIFY,
    # msg_count / score 为镜像聚合值（由流水表求和），正常业务不再写表；
    # 保留映射仅供管理员工具排查使用。
    "msg_count": F.C_MSG_COUNT,
    "score": F.C_SCORE,
})

ORGANIZER = TableMap(fields={
    "name": F.O_NAME,
    "phone": F.O_PHONE,
    "identity": F.O_IDENTITY,
    "binding_code": F.O_BINDING_CODE,
    "open_id": F.O_OPEN_ID,
    "verify_status": F.O_VERIFY,
})

TEAM = TableMap(
    fields={
        "team_no": F.T_NO,
        "reg_record_id": F.T_REG_RECORD,
        "preformed": F.T_PREFORMED,
        "agree_assign": F.T_AGREE_ASSIGN,
        "captain_ids": F.T_CAPTAIN,
        "member_ids": F.T_MEMBERS,
        "manual_member_ids": F.T_MANUAL_MEMBERS,
    },
    links=frozenset({"captain_ids", "member_ids", "manual_member_ids"}),
)

PROJECT = TableMap(
    fields={
        "project_no": F.J_NO,
        "name": F.J_NAME,
        "intro": F.J_INTRO,
        "repo_url": F.J_REPO,
        "demo_url": F.J_DEMO,
        "member_ids": F.J_MEMBERS,
        "team_ids": F.J_TEAM,
    },
    links=frozenset({"member_ids", "team_ids"}),
)

VOTE = TableMap(
    fields={"voter": F.V_VOTER, "project": F.V_PROJECT},
    links=frozenset({"voter", "project"}),
)

SCORE = TableMap(
    fields={"contestant": F.S_CONTESTANT, "delta": F.S_DELTA,
            "total_after": F.S_TOTAL_AFTER, "reason": F.S_REASON},
    links=frozenset({"contestant"}),
)

ACTIVITY = TableMap(
    fields={"contestant": F.A_CONTESTANT, "date": F.A_DATE, "msg_count": F.A_MSG_COUNT},
    links=frozenset({"contestant"}),
)

GROUP_CONFIG = TableMap(fields={
    "name": F.G_NAME,
    "chat_id": F.G_CHAT_ID,
    "enabled": F.G_ENABLED,
    "audiences": F.G_AUDIENCES,
    "note": F.G_NOTE,
})

PULL_LOG = TableMap(
    fields={"contestant": F.P_CONTESTANT, "chat_id": F.P_CHAT_ID,
            "group_name": F.P_GROUP_NAME, "result": F.P_RESULT,
            "fail_reason": F.P_FAIL_REASON},
    links=frozenset({"contestant"}),
)


def link_cell(ids: list[str]) -> list[dict]:
    """业务侧 record_id 列表 -> bitable link 写入形态。"""
    return [{"id": rid} for rid in (ids or []) if rid]


def encode(tmap: TableMap, fields: dict) -> dict:
    """业务键 dict -> 中文字段名 dict；link 键值整形为 [{"id":...}]。

    未登记的业务键抛 KeyError：写路径宁可失败也不静默丢写。
    """
    out: dict = {}
    for key, value in fields.items():
        name = tmap.fields.get(key)
        if name is None:
            raise KeyError(f"业务字段未在 field_map 登记（表映射 {tmap.fields and sorted(tmap.fields)[:3]}...）: {key}")
        if key in tmap.links:
            value = link_cell(value)
        out[name] = value
    return out
