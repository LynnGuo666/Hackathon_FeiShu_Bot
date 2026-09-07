import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from scripts.import_team_submissions import (
    _parse_submission,
    _validate_team_eligibility,
    choose_notification_recipient,
    notify_result,
    run,
)


def contestant(
    record_id, phone, *, open_id="", grade="大二", verify_status="已验证", email=""
):
    return types.SimpleNamespace(
        record_id=record_id,
        phone=phone,
        email=email,
        open_id=open_id,
        grade=grade,
        verify_status=verify_status,
    )


class TeamSubmissionImportTests(unittest.TestCase):
    def test_structured_teammates_follow_declared_count(self):
        record = {
            "fields": {
                "队长姓名": "队长",
                "队长手机号（自动校验）": "13800000000",
                "队长邮箱": "captain@example.com",
                "队友人数": 2,
                "队友1姓名": "队友",
                "队友1手机号": "13900000000",
                "队友1邮箱": "member@example.com",
                "队友2姓名": "队友二",
                "队友2手机号": "13700000000",
                "队友2邮箱": "member2@example.com",
            }
        }

        people = _parse_submission(record)

        self.assertEqual([person["phone"] for person in people], [
            "13800000000", "13900000000", "13700000000",
        ])

    def test_notification_prefers_captain_and_falls_back_to_first_teammate(self):
        people = [
            {"phone": "13800000000"},
            {"phone": "13900000000"},
            {"phone": "13700000000"},
        ]
        contestants = {
            "13800000000": contestant("r1", "13800000000", open_id=""),
            "13900000000": contestant("r2", "13900000000", open_id="ou_member"),
            "13700000000": contestant("r3", "13700000000", open_id="ou_later"),
        }

        recipient, role = choose_notification_recipient(people, contestants)

        self.assertEqual((recipient, role), ("ou_member", "队友"))

    def test_team_requires_freshman_and_verified_members(self):
        people = [{"phone": "13800000000"}, {"phone": "13900000000"}]
        contestants = {
            "13800000000": contestant("r1", "13800000000", grade="大二"),
            "13900000000": contestant("r2", "13900000000", grade="大三"),
        }

        with self.assertRaisesRegex(ValueError, "至少需要一名年级为大一"):
            _validate_team_eligibility(people, contestants)

        contestants["13800000000"].grade = "大一"
        contestants["13900000000"].verify_status = "未验证"
        with self.assertRaisesRegex(ValueError, "存在未入群选手"):
            _validate_team_eligibility(people, contestants)

    def test_team_rejects_unknown_phone(self):
        people = [{"phone": "13800000000"}]

        with self.assertRaisesRegex(ValueError, "未在选手表找到"):
            _validate_team_eligibility(people, {})

    def test_team_requires_three_to_five_people(self):
        base = {
            "队长姓名": "队长",
            "队长手机号（自动校验）": "13800000000",
            "队长邮箱": "captain@example.com",
        }
        for count in (0, 1):
            fields = {**base, "队友人数": count}
            expected = "总人数必须为3到5人" if count == 0 else "实际填写了0名队友"
            with self.assertRaisesRegex(ValueError, expected):
                _parse_submission({"fields": fields})

    def test_notify_result_sends_one_message_only(self):
        people = [{"phone": "13800000000"}, {"phone": "13900000000"}]
        contestants = {
            "13800000000": contestant("r1", "13800000000", open_id="ou_captain"),
            "13900000000": contestant("r2", "13900000000", open_id="ou_member"),
        }
        message = "组队失败 ❌\n\n失败原因：队伍没有大一新生"

        with patch(
            "scripts.import_team_submissions.send_text",
            new_callable=AsyncMock,
        ) as send_text:
            status = asyncio.run(notify_result(people, contestants, message, apply=True))

        send_text.assert_awaited_once_with("ou_captain", message)
        self.assertEqual(status, "已通知队长")

    def test_dry_run_does_not_send(self):
        with patch(
            "scripts.import_team_submissions.send_text",
            new_callable=AsyncMock,
        ) as send_text:
            status = asyncio.run(notify_result([], {}, "测试", apply=False))

        send_text.assert_not_awaited()
        self.assertEqual(status, "未发送（dry-run）")

    def test_successful_import_normalizes_contestant_phone_and_notifies_only_captain(self):
        """The success seam matches formatted contestant phones and never falls back."""
        record = {
            "record_id": "rec-success-001",
            "fields": {
                "队长姓名": "队长",
                "队长手机号（自动校验）": "13800000000",
                "队长邮箱": "captain@example.com",
                "队友人数": 2,
                "队友1姓名": "队友一",
                "队友1手机号": "13900000000",
                "队友1邮箱": "member1@example.com",
                "队友2姓名": "队友二",
                "队友2手机号": "13700000000",
                "队友2邮箱": "member2@example.com",
            },
        }
        store = types.SimpleNamespace(
            list_records=lambda _: [record],
            batch_update=lambda *_: None,
        )
        service = types.SimpleNamespace(
            contestants=types.SimpleNamespace(
                list_all=AsyncMock(return_value=[
                    contestant("c1", "+86 138 0000 0000", open_id="", grade="大一", email="captain@example.com"),
                    contestant("c2", "13900000000", open_id="ou_teammate", email="member1@example.com"),
                    contestant("c3", "13700000000", open_id="ou_other", email="member2@example.com"),
                ])
            ),
            teams=types.SimpleNamespace(
                list_all=AsyncMock(return_value=[]),
                create_many=AsyncMock(),
            ),
        )

        with (
            patch("scripts.import_team_submissions.init_services"),
            patch("scripts.import_team_submissions.BaseStore", return_value=store),
            patch("scripts.import_team_submissions.SVC", service),
            patch(
                "scripts.import_team_submissions.send_text",
                new_callable=AsyncMock,
            ) as send_text,
        ):
            exit_code = asyncio.run(run(apply=True))

        self.assertEqual(exit_code, 0)
        service.teams.create_many.assert_awaited_once()
        send_text.assert_not_awaited()

    def test_failed_import_keeps_a_contactable_captain_as_the_recipient(self):
        """A malformed captain field does not discard the captain's open_id."""
        record = {
            "record_id": "rec-failure-001",
            "fields": {
                "队长姓名": "队长",
                "队长手机号（自动校验）": "13800000000",
                "队长邮箱": "not-an-email",
                "队友人数": 2,
                "队友1姓名": "队友一",
                "队友1手机号": "13900000000",
                "队友1邮箱": "member1@example.com",
                "队友2姓名": "队友二",
                "队友2手机号": "13700000000",
                "队友2邮箱": "member2@example.com",
            },
        }
        store = types.SimpleNamespace(
            list_records=lambda _: [record],
            batch_update=lambda *_: None,
        )
        service = types.SimpleNamespace(
            contestants=types.SimpleNamespace(
                list_all=AsyncMock(return_value=[
                    contestant("c1", "13800000000", open_id="ou_captain"),
                    contestant("c2", "13900000000", open_id="ou_teammate"),
                ])
            ),
            teams=types.SimpleNamespace(list_all=AsyncMock(return_value=[])),
        )

        with (
            patch("scripts.import_team_submissions.init_services"),
            patch("scripts.import_team_submissions.BaseStore", return_value=store),
            patch("scripts.import_team_submissions.SVC", service),
            patch(
                "scripts.import_team_submissions.send_text",
                new_callable=AsyncMock,
            ) as send_text,
        ):
            exit_code = asyncio.run(run(apply=True))

        self.assertEqual(exit_code, 1)
        self.assertEqual(send_text.await_args.args[0], "ou_captain")

    def test_previously_failed_submission_is_not_notified_again(self):
        """The importer treats a recorded normal failure as already judged."""
        record = {
            "record_id": "rec-failure-002",
            "fields": {
                "导入状态": "导入失败",
                "导入结果": "队伍至少需要一名年级为大一的选手",
                "队长手机号（自动校验）": "13800000000",
            },
        }
        store = types.SimpleNamespace(
            list_records=lambda _: [record],
            batch_update=lambda *_: self.fail("already judged submissions must not be written"),
        )
        service = types.SimpleNamespace(
            contestants=types.SimpleNamespace(list_all=AsyncMock(return_value=[])),
            teams=types.SimpleNamespace(list_all=AsyncMock(return_value=[])),
        )

        with (
            patch("scripts.import_team_submissions.init_services"),
            patch("scripts.import_team_submissions.BaseStore", return_value=store),
            patch("scripts.import_team_submissions.SVC", service),
            patch(
                "scripts.import_team_submissions.send_text",
                new_callable=AsyncMock,
            ) as send_text,
        ):
            exit_code = asyncio.run(run(apply=True))

        self.assertEqual(exit_code, 0)
        send_text.assert_not_awaited()

    def test_one_team_violation_precedes_later_eligibility_failures(self):
        """The importer reports an existing team before grade or verification issues."""
        record = {
            "record_id": "rec-order-001",
            "fields": {
                "队长姓名": "队长",
                "队长手机号（自动校验）": "13800000000",
                "队长邮箱": "captain@example.com",
                "队友人数": 2,
                "队友1姓名": "队友一",
                "队友1手机号": "13900000000",
                "队友1邮箱": "member1@example.com",
                "队友2姓名": "队友二",
                "队友2手机号": "13700000000",
                "队友2邮箱": "member2@example.com",
            },
        }
        store = types.SimpleNamespace(
            list_records=lambda _: [record],
            batch_update=lambda *_: None,
        )
        service = types.SimpleNamespace(
            contestants=types.SimpleNamespace(
                list_all=AsyncMock(return_value=[
                    contestant("c1", "13800000000", open_id="ou_captain"),
                    contestant("c2", "13900000000"),
                    contestant("c3", "13700000000"),
                ])
            ),
            teams=types.SimpleNamespace(
                list_all=AsyncMock(return_value=[types.SimpleNamespace(
                    record_id="team-record-1",
                    reg_record_id="",
                    team_no="T-existing",
                    all_member_ids=["c1"],
                )]),
            ),
        )

        with (
            patch("scripts.import_team_submissions.init_services"),
            patch("scripts.import_team_submissions.BaseStore", return_value=store),
            patch("scripts.import_team_submissions.SVC", service),
            patch(
                "scripts.import_team_submissions.send_text",
                new_callable=AsyncMock,
            ) as send_text,
        ):
            exit_code = asyncio.run(run(apply=True))

        self.assertEqual(exit_code, 1)
        self.assertIn("已属于队伍 T-existing", send_text.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
