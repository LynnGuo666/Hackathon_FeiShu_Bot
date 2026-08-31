"""报名表 -> 选手数据库 同步器。

核心规则：
- 一个选手一个 ID：以归一化手机号为唯一键去重（去空格/横线/+86）。
- 同一手机号多次出现（主报名人 + 多个队伍的队友）只保留一条选手记录；
  字段合并时优先取主报名人行，其次先到先得。
- 队友关系按报名记录写入队伍表（队长=主报名人，队友=link 到选手）。
- 增量：按手机号比对已有选手，已存在则更新，否则分配新 选手ID（WY01-XXXX）。
"""
from __future__ import annotations

import logging
import re

from ...core.base_store import BaseStore
from ...core.config import CFG

log = logging.getLogger(__name__)

ROLES = ["主报名人", "队友1", "队友2", "队友3", "队友4"]
ROLE_FIELD_PREFIX = {"主报名人": "", "队友1": "队友1 ", "队友2": "队友2 ", "队友3": "队友3 ", "队友4": "队友4 "}


def normalize_phone(raw) -> str:
    s = _text(raw)
    digits = re.sub(r"\D", "", s)
    if digits.startswith("86") and len(digits) > 11:
        digits = digits[2:]
    return digits


# 大陆手机号：1 开头、第二位 3-9、共 11 位
VALID_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def is_valid_phone(phone: str) -> bool:
    return bool(VALID_PHONE_RE.match(phone))


def _text(cell) -> str:
    """bitable 文本字段可能是字符串或 [{text:...}] 分段数组。"""
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, list):
        parts = []
        for seg in cell:
            if isinstance(seg, dict):
                parts.append(str(seg.get("text", "")))
            else:
                parts.append(str(seg))
        return "".join(parts).strip()
    return str(cell).strip()


def _select_one(cell) -> str:
    if isinstance(cell, list):
        return _text(cell[0]) if cell else ""
    return _text(cell)


def _select_many(cell) -> list[str]:
    if isinstance(cell, list):
        return [_text(x) for x in cell if _text(x)]
    s = _text(cell)
    return [s] if s else []


def extract_appearances(record_fields: dict) -> list[dict]:
    """从一条报名记录提取所有人（手机号无效的直接丢弃）：[{role, name, phone, ...}]"""
    out = []
    for role in ROLES:
        p = ROLE_FIELD_PREFIX[role]
        phone = normalize_phone(record_fields.get(f"{p}手机号"))
        if not is_valid_phone(phone):
            continue
        out.append({
            "role": role,
            "name": _text(record_fields.get(f"{p}姓名")),
            "phone": phone,
            "vx": _text(record_fields.get(f"{p}vx号")),
            "school": _text(record_fields.get(f"{p}学校")),
            "major": _text(record_fields.get(f"{p}专业")),
            "grade": _select_one(record_fields.get(f"{p}年级")),
            "identity": _select_one(record_fields.get(f"{p}身份")),
            "intent": _select_many(record_fields.get(f"{p}意向角色")),
        })
    return out


def merge_contestants(reg_records: list[dict]) -> dict[str, dict]:
    """按手机号合并所有人 -> {phone: {fields..., appearances: [...]}}"""
    people: dict[str, dict] = {}
    for rec in reg_records:
        fields = rec.get("fields") or {}
        record_id = rec.get("record_id")
        for app in extract_appearances(fields):
            p = people.setdefault(app["phone"], {"appearances": []})
            is_owner = app["role"] == "主报名人"
            # 主报名人信息优先；否则只补空缺字段
            def pick(key, val):
                if is_owner or not p.get(key):
                    p[key] = val

            pick("姓名", app["name"])
            pick("vx号", app["vx"])
            pick("学校", app["school"])
            pick("专业", app["major"])
            pick("年级", app["grade"])
            pick("身份", app["identity"])
            if is_owner or not p.get("意向角色"):
                p["意向角色"] = app["intent"]
            # 审核状态：任一队伍「审核通过」即通过；否则任一「审核不通过」即不通过；否则未审核
            st = _select_one(fields.get("审核状态")) or "未审核"
            cur = p.get("审核状态")
            if st == "审核通过" or cur is None:
                p["审核状态"] = st
            elif cur != "审核通过" and st == "审核不通过":
                p["审核状态"] = st
            p["appearances"].append({
                "record_id": record_id,
                "role": app["role"],
                "预组队": _select_one(fields.get("是否预组队")) or "",
                "统一分配": _select_one(fields.get("是否同意统一分配")) or "",
            })
    return people


def run_sync(store: BaseStore | None = None, reg_store: BaseStore | None = None) -> dict:
    """执行一次同步，返回统计信息。"""
    store = store or BaseStore(CFG.db_base_token)
    reg_store = reg_store or BaseStore(CFG.reg_base_token)

    reg_records = reg_store.list_records(CFG.reg_table_id)
    people = merge_contestants(reg_records)

    existing = store.list_records(CFG.tbl_contestants)
    by_phone: dict[str, dict] = {}
    max_seq = 0
    for r in existing:
        f = r.get("fields") or {}
        ph = normalize_phone(f.get("手机号"))
        if ph:
            by_phone[ph] = r
            # 兼容旧格式 WY-0001 与新格式 WY01-0001
            m = re.match(r"WY(?:01)?-(\d+)", _text(f.get("选手ID")))
            if m:
                max_seq = max(max_seq, int(m.group(1)))

    to_create, to_update = [], []
    for phone, p in sorted(people.items()):
        row = {
            "姓名": p.get("姓名", ""),
            "手机号": phone,
            "vx号": p.get("vx号", ""),
            "学校": p.get("学校", ""),
            "专业": p.get("专业", ""),
            "审核状态": p.get("审核状态", "未审核"),
        }
        if p.get("年级"):
            row["年级"] = p["年级"]
        if p.get("身份"):
            row["身份"] = p["身份"]
        if p.get("意向角色"):
            row["意向角色"] = p["意向角色"]
        old = by_phone.get(phone)
        if old:
            oldf = old.get("fields") or {}

            def changed(k, v):
                cur = oldf.get(k)
                if isinstance(cur, list):  # select 字段读回是 [选项] 形式
                    cur = cur[0] if len(cur) == 1 else cur
                if cur in (None, "") and (v == "" or v == []):  # 空值等价，避免反复清写
                    return False
                if isinstance(v, list):  # 多选字段读回/写入都是列表
                    return sorted(map(str, cur if isinstance(cur, list) else [cur])) != sorted(map(str, v)) if cur is not None else True
                return cur != v

            diff = {k: v for k, v in row.items() if changed(k, v)}
            if diff:
                to_update.append({"record_id": old["record_id"], "fields": diff})
        else:
            max_seq += 1
            row["选手ID"] = f"WY01-{max_seq:04d}"
            row["验证状态"] = "未验证"
            row["入群状态"] = "未入群"
            to_create.append(row)

    if to_create:
        store.batch_create(CFG.tbl_contestants, to_create)
    if to_update:
        store.batch_update(CFG.tbl_contestants, to_update)

    # 重新读取选手表建立 phone -> record_id 映射（含新建）
    contestants = {normalize_phone(r["fields"].get("手机号")): r["record_id"]
                   for r in store.list_records(CFG.tbl_contestants) if normalize_phone((r.get("fields") or {}).get("手机号"))}

    # 队伍表 upsert（按 报名记录ID）：只有明确预组队（是否预组队=是）的报名才进队伍表
    team_rows = {}
    for rec in reg_records:
        fields = rec.get("fields") or {}
        rid = rec.get("record_id")
        if _select_one(fields.get("是否预组队")) != "是":
            continue
        apps = extract_appearances(fields)
        if not apps:
            continue
        owner = next((a for a in apps if a["role"] == "主报名人"), None)
        teammates = [a for a in apps if a["role"] != "主报名人"]
        team_rows[rid] = {
            "队伍ID": f"T-{rid[-8:]}" if rid else "",
            "报名记录ID": rid or "",
            "是否预组队": "是",
            "同意统一分配": _select_one(fields.get("是否同意统一分配")) or "否",
            "_owner": contestants.get(owner["phone"]) if owner else None,
            "_members": [contestants[a["phone"]] for a in teammates if a["phone"] in contestants],
        }
    existing_teams = {r["fields"].get("报名记录ID"): r for r in store.list_records(CFG.tbl_teams)
                      if (r.get("fields") or {}).get("报名记录ID")}
    t_create, t_update = [], []
    for rid, t in team_rows.items():
        linkf = {"队长": [{"id": t["_owner"]}] if t["_owner"] else [],
                 "队友": [{"id": x} for x in t["_members"]]}
        if rid in existing_teams:
            t_update.append({"record_id": existing_teams[rid]["record_id"], "fields": linkf})
        else:
            t_create.append({"队伍ID": t["队伍ID"], "报名记录ID": t["报名记录ID"],
                             "是否预组队": t["是否预组队"], "同意统一分配": t["同意统一分配"], **linkf})
    if t_create:
        store.batch_create(CFG.tbl_teams, t_create)
    if t_update:
        store.batch_update(CFG.tbl_teams, t_update)

    stats = {"报名记录": len(reg_records), "选手(去重后)": len(people),
             "新建选手": len(to_create), "更新选手": len(to_update),
             "预组队队伍": len(team_rows), "无效手机号跳过的人次": _count_invalid(reg_records)}
    log.info("同步完成: %s", stats)
    return stats


def _count_invalid(reg_records: list[dict]) -> int:
    n = 0
    for rec in reg_records:
        for role in ROLES:
            p = ROLE_FIELD_PREFIX[role]
            raw = normalize_phone(rec["fields"].get(f"{p}手机号"))
            if raw and not is_valid_phone(raw):
                n += 1
    return n
