#!/usr/bin/env python3
"""把「组队收集表」的队长提交导入选手表和队伍表。

默认只做校验和预览；加 --apply 才会写入。
每条提交对应一条队伍：队长写入「队长」，队友写入「手动队友」，
这样不会被报名表同步覆盖。只有已存在且已验证的选手才能导入；
成功仅向队长发送结果；普通失败时，队长没有可用身份才向首个可用队友兜底。
"""
from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.adapters.feishu import cells  # noqa: E402
from bot.core.base_store import BaseStore  # noqa: E402
from bot.core.config import CFG  # noqa: E402
from bot.core.lark_client import send_text  # noqa: E402
from bot.core.service import SVC, init_services  # noqa: E402
from bot.modules.sync.sync import is_valid_phone, normalize_phone  # noqa: E402


TBL_SUBMISSIONS = "组队收集表"
STATUS = "导入状态"
RESULT = "导入结果"
VERIFIED = "已验证"
FRESHMAN = "大一"
FORM_FIELDS = {
    "captain": ("队长姓名", "队长手机号（自动校验）", "队长邮箱"),
    "teammate_count": "队伍人数",
    "teammate_blob": "队友信息（可选）",
    "teammates": [
        (f"队友{i}姓名", f"队友{i}手机号", f"队友{i}邮箱")
        for i in range(1, 5)
    ],
}


def _text(value) -> str:
    return cells.text(value).strip()


def _read_person(fields: dict, names: tuple[str, str, str], label: str) -> dict | None:
    name, phone, email = (_text(fields.get(key)) for key in names)
    if not any((name, phone, email)):
        return None
    if not all((name, phone, email)):
        raise ValueError(f"{label}的姓名、手机号、邮箱必须同时填写")
    normalized = normalize_phone(phone)
    if not is_valid_phone(normalized):
        raise ValueError(f"{label}手机号格式不正确")
    if "@" not in email:
        raise ValueError(f"{label}邮箱格式不正确")
    return {"name": name, "phone": normalized, "email": email}


def _read_teammates(value) -> list[dict]:
    raw = _text(value)
    if not raw:
        return []
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) > 4:
        raise ValueError("队友最多填写4人")
    people = []
    for index, line in enumerate(lines, 1):
        parts = [part.strip() for part in re.split(r"\s*[|｜,，\t]\s*", line)]
        if len(parts) != 3:
            raise ValueError(f"队友{index}格式不正确，应为：姓名 | 手机号 | 邮箱")
        person = _read_person(
            dict(zip(("name", "phone", "email"), parts)),
            ("name", "phone", "email"),
            f"队友{index}",
        )
        if person:
            people.append(person)
    return people


def _read_structured_teammates(fields: dict) -> list[dict]:
    people = []
    for index, names in enumerate(FORM_FIELDS["teammates"], 1):
        person = _read_person(fields, names, f"队友{index}")
        if person:
            people.append(person)
    return people


def _is_empty_submission(fields: dict) -> bool:
    return not any(
        _text(value)
        for key, value in fields.items()
        if key not in {STATUS, RESULT}
    )


def _match_team_members(people: list[dict], contestants_by_phone: dict) -> list[tuple[dict, object]]:
    """Match submitted members to contestants before applying eligibility gates."""
    matched = []
    missing = []
    for person in people:
        contestant = contestants_by_phone.get(person["phone"])
        if contestant is None:
            missing.append(person["phone"])
        else:
            matched.append((person, contestant))
    if missing:
        raise ValueError(f"手机号未在选手表找到，无法确认入群状态：{', '.join(missing)}")
    return matched


def _validate_matched_team_eligibility(matched: list[tuple[dict, object]]) -> list[tuple[dict, object]]:
    """Apply the post-membership eligibility gates in the specified order."""
    if not any(contestant.grade == FRESHMAN for _, contestant in matched):
        raise ValueError("队伍至少需要一名年级为大一的选手")

    unverified = [
        person["phone"]
        for person, contestant in matched
        if contestant.verify_status != VERIFIED
    ]
    if unverified:
        raise ValueError(f"队伍中存在未入群选手（验证状态不是已验证）：{', '.join(unverified)}")
    return [(person, contestant.record_id) for person, contestant in matched]


def _validate_team_eligibility(people: list[dict], contestants_by_phone: dict) -> list[tuple[dict, object]]:
    """Validate submitted members against the contestant eligibility gates."""
    return _validate_matched_team_eligibility(
        _match_team_members(people, contestants_by_phone)
    )


def classify_team_change(new_member_ids: list[str], captain_id: str, teams: list) -> tuple[str, object | None]:
    """Classify a new submission against teams sharing one or more members."""
    new_members = set(new_member_ids)
    matches = [team for team in teams if new_members & set(team.all_member_ids)]
    if not matches:
        return "new", None
    if len(matches) != 1:
        return "multi-team-match", None
    team = matches[0]
    old_members = set(team.all_member_ids)
    if captain_id not in set(team.captain_ids):
        return "captain-changed", team
    if new_members == old_members:
        return "unchanged", team
    if old_members < new_members:
        return "add-members", team
    if new_members < old_members:
        return "members-removed", team
    return "members-replaced", team


def _parse_submission(record: dict) -> list[dict]:
    fields = record.get("fields") or {}
    captain = _read_person(fields, FORM_FIELDS["captain"], "队长")
    if captain is None:
        raise ValueError("队长信息不能为空")
    structured_teammates = _read_structured_teammates(fields)
    blob_teammates = _read_teammates(fields.get(FORM_FIELDS["teammate_blob"]))
    if structured_teammates and blob_teammates:
        raise ValueError("不能同时填写结构化队友字段和队友信息文本框")
    teammates = structured_teammates or blob_teammates
    count_text = _text(fields.get(FORM_FIELDS["teammate_count"]))
    if not count_text:
        raise ValueError("队友人数不能为空")
    try:
        teammate_count = int(float(count_text))
    except ValueError as exc:
        raise ValueError("队友人数必须是0到4之间的整数") from exc
    if not 0 <= teammate_count <= 4:
        raise ValueError("队友人数必须是0到4之间的整数")
    if teammate_count != len(teammates):
        raise ValueError(f"队友人数填写为{teammate_count}，但实际填写了{len(teammates)}名队友")
    people = [captain, *teammates]
    if not 3 <= len(people) <= 5:
        raise ValueError("队伍总人数必须为3到5人")
    phones = [p["phone"] for p in people]
    if len(set(phones)) != len(phones):
        raise ValueError("同一队伍内手机号重复")
    return people


def choose_notification_recipient(
    people: list[dict], contestants_by_phone: dict
) -> tuple[str | None, str | None]:
    """选择唯一通知对象：队长优先，随后按队友顺序兜底。"""
    for index, person in enumerate(people):
        contestant = contestants_by_phone.get(person["phone"])
        if contestant is not None and contestant.open_id:
            return contestant.open_id, "队长" if index == 0 else "队友"
    return None, None


def _best_effort_person(fields: dict, names: tuple[str, str, str]) -> dict | None:
    """Extract a contactable person even when another submitted field is invalid."""
    name, phone, email = (_text(fields.get(key)) for key in names)
    normalized = normalize_phone(phone)
    if not is_valid_phone(normalized):
        return None
    return {"name": name, "phone": normalized, "email": email}


def _best_effort_people(record: dict) -> list[dict]:
    """失败时尽量提取手机号，用于向队长或队友发送失败原因。"""
    fields = record.get("fields") or {}
    people = []
    captain = _best_effort_person(fields, FORM_FIELDS["captain"])
    if captain:
        people.append(captain)
    try:
        structured = _read_structured_teammates(fields)
    except ValueError:
        structured = []
    try:
        blob = _read_teammates(fields.get(FORM_FIELDS["teammate_blob"]))
    except ValueError:
        blob = []
    return people + (structured or blob)


async def notify_result(
    people: list[dict],
    contestants_by_phone: dict,
    message: str,
    apply: bool,
    *,
    fallback_to_teammate: bool = True,
) -> str:
    """发送单条结果消息；dry-run 不发送，发送失败不影响导入结果。"""
    if not apply:
        return "未发送（dry-run）"
    if fallback_to_teammate:
        open_id, role = choose_notification_recipient(people, contestants_by_phone)
    else:
        captain = people[0] if people else None
        contestant = contestants_by_phone.get(captain["phone"]) if captain else None
        open_id = contestant.open_id if contestant else None
        role = "队长"
    if not open_id:
        if fallback_to_teammate:
            return "未发送：队长和队友均没有可用飞书身份"
        return "未发送：队长没有可用飞书身份"
    try:
        await send_text(open_id, message)
    except Exception as exc:  # notification failure must not roll back team import
        return f"通知失败（{role}）：{exc}"
    return f"已通知{role}"


async def notify_admin_conflict(record_id: str, change_kind: str, team, people: list[dict], apply: bool) -> str:
    """Notify configured administrators without changing conflict handling on failure."""
    if not apply:
        return "未发送（dry-run）"
    old_team = team.team_no if team else "无"
    old_members = ", ".join(team.all_member_ids) if team else "无"
    new_members = ", ".join(person["phone"] for person in people)
    message = (
        "组队登记需管理员确认\n\n"
        f"登记记录 ID：{record_id}\n冲突类型：{change_kind}\n"
        f"原队伍：{old_team}\n原成员：{old_members}\n"
        f"新提交队长：{people[0]['phone']}\n新提交成员：{new_members}"
    )
    failures = []
    for open_id in CFG.admin_open_ids:
        try:
            await send_text(open_id, message)
        except Exception as exc:
            failures.append(str(exc))
    return "管理员通知失败：" + "; ".join(failures) if failures else "已通知管理员"


async def run(apply: bool) -> int:
    init_services()
    store = BaseStore(CFG.db_base_token)
    submissions = await asyncio.to_thread(store.list_records, TBL_SUBMISSIONS)
    contestants = await SVC.contestants.list_all()
    contestants_by_phone = {}
    for contestant in contestants:
        phone = normalize_phone(contestant.phone)
        if phone:
            contestants_by_phone[phone] = contestant
    teams = await SVC.teams.list_all()
    team_by_source = {t.reg_record_id: t for t in teams if t.reg_record_id}
    occupied: dict[str, str] = {}
    for team in teams:
        for rid in team.all_member_ids:
            occupied[rid] = team.team_no or team.record_id

    imported = skipped = failed = 0
    reserved: dict[str, str] = {}
    for record in submissions:
        record_id = record.get("record_id", "")
        fields = record.get("fields") or {}
        status = _text(fields.get(STATUS))
        if status in {"已导入", "已跳过", "导入失败", "需管理员确认"}:
            skipped += 1
            continue
        if _is_empty_submission(fields):
            skipped += 1
            if apply:
                await asyncio.to_thread(store.batch_update, TBL_SUBMISSIONS, [{
                    "record_id": record_id,
                    "fields": {STATUS: "已跳过", RESULT: "空提交，未导入"},
                }])
            continue
        source_id = f"FORM-{record_id}"
        if source_id in team_by_source:
            skipped += 1
            if apply:
                await asyncio.to_thread(store.batch_update, TBL_SUBMISSIONS, [{
                    "record_id": record_id,
                    "fields": {STATUS: "已导入", RESULT: "队伍表已存在，跳过重复导入"},
                }])
            continue
        team_created = False
        try:
            people = _parse_submission(record)
            matched_people = _match_team_members(people, contestants_by_phone)
            member_ids = [contestant.record_id for _, contestant in matched_people]
            change_kind, matched_team = classify_team_change(
                member_ids, member_ids[0], teams
            )
            if change_kind == "unchanged":
                skipped += 1
                if apply:
                    await asyncio.to_thread(store.batch_update, TBL_SUBMISSIONS, [{
                        "record_id": record_id,
                        "fields": {STATUS: "已跳过", RESULT: "与已有队伍完全一致，无需变更"},
                    }])
                continue
            person_ids = _validate_matched_team_eligibility(matched_people)
            if change_kind == "add-members":
                if apply:
                    updated_members = list(dict.fromkeys(
                        matched_team.manual_member_ids + [
                            rid for rid in member_ids if rid not in matched_team.all_member_ids
                        ]
                    ))
                    await SVC.teams.batch_update([(
                        matched_team.record_id, {"manual_member_ids": updated_members}
                    )])
                    await asyncio.to_thread(store.batch_update, TBL_SUBMISSIONS, [{
                        "record_id": record_id,
                        "fields": {STATUS: "已导入", RESULT: f"已为 {matched_team.team_no} 新增队友"},
                    }])
                imported += 1
                continue
            if change_kind not in {"new", "unchanged", "add-members"}:
                skipped += 1
                if apply:
                    notification = await notify_admin_conflict(
                        record_id, change_kind, matched_team, people, apply=True
                    )
                    await asyncio.to_thread(store.batch_update, TBL_SUBMISSIONS, [{
                        "record_id": record_id,
                        "fields": {STATUS: "需管理员确认", RESULT: f"结构性冲突：{change_kind}；{notification}"},
                    }])
                continue
            for person, contestant in matched_people:
                owner = occupied.get(contestant.record_id) or reserved.get(contestant.record_id)
                if owner:
                    raise ValueError(f"{person['phone']} 已属于队伍 {owner}")

            if apply:
                for person, rid in person_ids:
                    if person["email"] != contestants_by_phone[person["phone"]].email:
                        await SVC.contestants.batch_update([(rid, {"email": person["email"]})])
                captain_id = person_ids[0][1]
                member_ids = [rid for _, rid in person_ids[1:] if rid]
                team_row = {
                    "team_no": f"T-{record_id[-8:]}",
                    "reg_record_id": source_id,
                    "preformed": "是",
                    "agree_assign": "否",
                    "captain_ids": [captain_id],
                    "member_ids": [],
                    "manual_member_ids": member_ids,
                }
                await SVC.teams.create_many([team_row])
                team_created = True
                for _, rid in person_ids:
                    reserved[rid] = team_row["team_no"]
                await asyncio.to_thread(store.batch_update, TBL_SUBMISSIONS, [{
                    "record_id": record_id,
                    "fields": {STATUS: "已导入", RESULT: f"已导入 {team_row['team_no']}"},
                }])
                notification = await notify_result(
                    people,
                    contestants_by_phone,
                    "完成验证 ✅\n\n组队成功！",
                    apply=True,
                    fallback_to_teammate=False,
                )
                try:
                    await asyncio.to_thread(store.batch_update, TBL_SUBMISSIONS, [{
                        "record_id": record_id,
                        "fields": {
                            STATUS: "已导入",
                            RESULT: f"已导入 {team_row['team_no']}；{notification}",
                        },
                    }])
                except Exception as exc:
                    print(record_id, "WARN", f"队伍已导入，但通知结果回写失败: {exc}")
            else:
                notification = await notify_result(
                    people,
                    contestants_by_phone,
                    "完成验证 ✅\n\n组队成功！",
                    apply=False,
                    fallback_to_teammate=False,
                )
            imported += 1
            print(record_id, "OK", len(people), "人", notification)
        except Exception as exc:  # per-row failure should not block other submissions
            message = str(exc)
            if team_created:
                notification = await notify_result(
                    _best_effort_people(record),
                    contestants_by_phone,
                    "完成验证 ✅\n\n组队成功！",
                    apply=apply,
                    fallback_to_teammate=False,
                )
                recovery = f"队伍已创建，但后续处理失败：{message}；{notification}"
                print(record_id, "WARN", recovery)
                if apply:
                    try:
                        await asyncio.to_thread(store.batch_update, TBL_SUBMISSIONS, [{
                            "record_id": record_id,
                            "fields": {STATUS: "已导入", RESULT: recovery[:500]},
                        }])
                    except Exception as recovery_exc:
                        print(record_id, "WARN", f"队伍已创建，但结果回写失败: {recovery_exc}")
                imported += 1
                continue

            failed += 1
            print(record_id, "ERROR", message)
            notification = await notify_result(
                _best_effort_people(record),
                contestants_by_phone,
                f"组队失败 ❌\n\n失败原因：{message}",
                apply=apply,
            )
            if apply:
                await asyncio.to_thread(store.batch_update, TBL_SUBMISSIONS, [{
                    "record_id": record_id,
                    "fields": {
                        STATUS: "导入失败",
                        RESULT: f"{message[:400]}；{notification}"[:500],
                    },
                }])
    print(f"summary imported={imported} skipped={skipped} failed={failed} apply={apply}")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="执行写入；默认只校验预览")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.apply)))


if __name__ == "__main__":
    main()
