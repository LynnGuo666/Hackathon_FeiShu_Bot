"""报名表 -> 选手数据库 同步器。

核心规则：
- 一个选手一个 ID：以归一化手机号为唯一键去重（去空格/横线/+86）。
- 同一手机号多次出现（主报名人 + 多个队伍的队友）只保留一条选手记录；
  字段合并时优先取主报名人行，其次先到先得。
- 队友关系按报名记录写入队伍表（队长=主报名人，队友=link 到选手）。
- 增量：按手机号比对已有选手，已存在则更新，否则分配新 选手ID（WY01-XXXX）。

报名表字段结构（角色前缀 + 中文列名）是报名表单的领域知识，保留在本模块；
选手库写入一律经 SVC 仓库接口（core.service）。
"""
from __future__ import annotations

import asyncio
import logging
import re

from ...core.models import Registration
from ...core.plugin import Plugin
from ...core.registry import REGISTRY
from ...core.service import SVC

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
    """bitable 文本字段可能是字符串或 [{text:...}] 分段数组（报名表原始字段仍需归一化）。"""
    from ...adapters.feishu.cells import text
    return text(cell)


def _select_one(cell) -> str:
    from ...adapters.feishu.cells import select_one
    return select_one(cell)


def _select_many(cell) -> list[str]:
    from ...adapters.feishu.cells import select_many
    return select_many(cell)


def audit_status_of(audit_cell) -> str:
    """审核状态读回可能是字符串或 [选项] 列表，统一取字符串。"""
    return _select_one(audit_cell)


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


def merge_contestants(registrations: list[Registration]) -> dict[str, dict]:
    """按手机号合并所有人 -> {phone: {fields..., appearances: [...]}}"""
    people: dict[str, dict] = {}
    for reg in registrations:
        fields = reg.fields
        record_id = reg.record_id
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
            # 报名记录ID：主报名人行优先（选手表回溯报名用）
            pick("报名记录ID", record_id)
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


# 全局互斥：定时同步与手动「同步」并发跑会基于同一快照各自新建选手，
# 破坏「手机号唯一键」不变量，因此同一时间只允许一个同步在运行。
_sync_lock = asyncio.Lock()


def sync_in_progress() -> bool:
    return _sync_lock.locked()


async def run_sync() -> dict:
    """执行一次同步，返回统计信息（并发调用会串行等待）。"""
    async with _sync_lock:
        return await _run_sync_impl()


async def sync_job() -> None:
    """定时同步任务：上一轮未结束时跳过本轮。"""
    if sync_in_progress():
        log.info("上一轮同步仍在进行，跳过本轮定时同步")
        return
    await run_sync()
    from ..sync.audience import sync_group_audience_options
    await sync_group_audience_options()


async def _run_sync_impl() -> dict:
    registrations = await SVC.registrations.list_all()
    people = merge_contestants(registrations)

    existing = await SVC.contestants.list_all()
    by_phone: dict[str, object] = {}
    max_seq = 0
    for c in existing:
        if c.phone:
            by_phone[c.phone] = c
            # 兼容旧格式 WY-0001 与新格式 WY01-0001
            m = re.match(r"WY(?:01)?-(\d+)", c.contestant_no)
            if m:
                max_seq = max(max_seq, int(m.group(1)))

    to_create, to_update = [], []
    for phone, p in sorted(people.items()):
        row = {
            "name": p.get("姓名", ""),
            "phone": phone,
            "vx": p.get("vx号", ""),
            "school": p.get("学校", ""),
            "major": p.get("专业", ""),
            "audit_status": p.get("审核状态", "未审核"),
        }
        if p.get("年级"):
            row["grade"] = p["年级"]
        if p.get("身份"):
            row["identity"] = p["身份"]
        if p.get("意向角色"):
            row["intent_roles"] = p["意向角色"]
        if p.get("报名记录ID"):
            row["reg_record_id"] = p["报名记录ID"]
        old = by_phone.get(phone)
        if old is not None:
            diff = {k: v for k, v in row.items() if _field_changed(old, k, v)}
            if diff:
                to_update.append((old.record_id, diff))
        else:
            max_seq += 1
            row["contestant_no"] = f"WY01-{max_seq:04d}"
            row["verify_status"] = "未验证"
            to_create.append(row)

    if to_create:
        await SVC.contestants.create_many(to_create)
    if to_update:
        await SVC.contestants.batch_update(to_update)

    # 重新读取选手表建立 phone -> record_id 映射（含新建）
    contestants = {c.phone: c.record_id for c in await SVC.contestants.list_all() if c.phone}

    # 队伍表 upsert（按 报名记录ID）：只有明确预组队（是否预组队=是）的报名才进队伍表
    team_rows: dict[str, dict] = {}
    for reg in registrations:
        fields = reg.fields
        rid = reg.record_id
        if _select_one(fields.get("是否预组队")) != "是":
            continue
        apps = extract_appearances(fields)
        if not apps:
            continue
        owner = next((a for a in apps if a["role"] == "主报名人"), None)
        teammates = [a for a in apps if a["role"] != "主报名人"]
        team_rows[rid] = {
            "team_no": f"T-{rid[-8:]}" if rid else "",
            "reg_record_id": rid or "",
            "preformed": "是",
            "agree_assign": _select_one(fields.get("是否同意统一分配")) or "否",
            "_owner": contestants.get(owner["phone"]) if owner else None,
            "_members": [contestants[a["phone"]] for a in teammates if a["phone"] in contestants],
        }
    existing_teams = {t.reg_record_id: t for t in await SVC.teams.list_all() if t.reg_record_id}
    t_create, t_update = [], []
    for rid, t in team_rows.items():
        captain = [t["_owner"]] if t["_owner"] else []
        members = list(t["_members"])
        if rid in existing_teams:
            # 状态字段只在为空时补写（自愈历史缺口），不覆盖已有值
            old = existing_teams[rid]
            patch = {"captain_ids": captain, "member_ids": members}
            if not old.preformed:
                patch["preformed"] = t["preformed"]
            if not old.agree_assign:
                patch["agree_assign"] = t["agree_assign"]
            t_update.append((old.record_id, patch))
        else:
            t_create.append({"team_no": t["team_no"], "reg_record_id": t["reg_record_id"],
                             "preformed": t["preformed"], "agree_assign": t["agree_assign"],
                             "captain_ids": captain, "member_ids": members})
    if t_create:
        await SVC.teams.create_many(t_create)
    if t_update:
        await SVC.teams.batch_update(t_update)

    stats = {"报名记录": len(registrations), "选手(去重后)": len(people),
             "新建选手": len(to_create), "更新选手": len(to_update),
             "预组队队伍": len(team_rows), "无效手机号跳过的人次": _count_invalid(registrations)}
    log.info("同步完成: %s", stats)
    return stats


def _field_changed(old, key: str, value) -> bool:
    """与已有选手实体比较字段是否变化（空值等价，避免反复清写）。"""
    cur = getattr(old, key, None)
    if cur is None:
        cur = ""
    if isinstance(cur, list) or isinstance(value, list):
        return sorted(map(str, cur if isinstance(cur, list) else [cur])) != \
               sorted(map(str, value if isinstance(value, list) else [value]))
    if cur in ("", None) and (value == "" or value == []):
        return False
    return cur != value


def _count_invalid(registrations: list[Registration]) -> int:
    n = 0
    for reg in registrations:
        for role in ROLES:
            p = ROLE_FIELD_PREFIX[role]
            raw = normalize_phone(reg.fields.get(f"{p}手机号"))
            if raw and not is_valid_phone(raw):
                n += 1
    return n


class SyncPlugin(Plugin):
    """报名表同步：只注册定时任务；手动「同步」指令由 admin 插件注册。"""
    name = "sync"
    dependencies = ()

    def setup(self) -> None:
        from ...core.config import CFG
        REGISTRY.job("报名表自动同步", CFG.sync_interval_minutes, plugin=self.name)(sync_job)
