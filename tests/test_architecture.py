"""新增架构层的单元测试：实体映射、重试限流、投票防双投、活跃度落库幂等。"""
import asyncio
import threading
import time
import unittest
import unittest.mock
import types

from bot.adapters.feishu import cells
from bot.core.base_store import _error_code_of, _with_retry, _RETRYABLE_CODES
from bot.core.models import Contestant, Organizer, Project


class CellsTests(unittest.TestCase):
    """单元格归一化（唯一实现）的读回形态覆盖。"""

    def test_text_forms(self):
        self.assertEqual(cells.text(None), "")
        self.assertEqual(cells.text(" x "), "x")
        self.assertEqual(cells.text([{"text": "你好"}, {"text": "世界"}]), "你好世界")
        self.assertEqual(cells.text([1, 2]), "12")

    def test_select_forms(self):
        self.assertEqual(cells.select_one(["审核通过"]), "审核通过")
        self.assertEqual(cells.select_one("审核通过"), "审核通过")
        self.assertEqual(cells.select_many(["a", "b"]), ["a", "b"])
        self.assertEqual(cells.select_many("a"), ["a"])
        self.assertEqual(cells.select_many(None), [])

    def test_link_ids_forms(self):
        self.assertEqual(cells.link_ids([{"id": "rec1"}, {"id": "rec2"}]), ["rec1", "rec2"])
        self.assertEqual(cells.link_ids([{"record_ids": ["rec1"]}]), ["rec1"])
        self.assertEqual(cells.link_ids(None), [])
        self.assertEqual(cells.link_ids("x"), [])

    def test_to_int(self):
        self.assertEqual(cells.to_int(3), 3)
        self.assertEqual(cells.to_int(3.9), 3)
        self.assertEqual(cells.to_int("5"), 5)
        self.assertEqual(cells.to_int([{"text": "7"}]), 7)
        self.assertEqual(cells.to_int(None), 0)
        self.assertEqual(cells.to_int("abc"), 0)


class RetryTests(unittest.TestCase):
    """限流重试：可重试错误码指数退避，其他错误直接抛。"""

    def test_retry_on_rate_limit_code(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError(f"SDK 批量新建失败: {next(iter(_RETRYABLE_CODES))} too many")
            return "ok"

        self.assertEqual(_with_retry(flaky, "t"), "ok")
        self.assertEqual(calls["n"], 3)

    def test_no_retry_on_other_codes(self):
        calls = {"n": 0}

        def bad():
            calls["n"] += 1
            raise RuntimeError("SDK 读取失败: 230001 bad token")

        with self.assertRaises(RuntimeError):
            _with_retry(bad, "t")
        self.assertEqual(calls["n"], 1)

    def test_error_code_parsing(self):
        self.assertEqual(_error_code_of(RuntimeError("SDK 读取失败: 99991400 too many")), 99991400)
        self.assertIsNone(_error_code_of(RuntimeError("no code")))


class FakeVoteRepos:
    """投票防双投测试的假仓库：内存实现，与 Protocol 同契约。"""

    def __init__(self, contestants, projects):
        self.contestants = contestants
        self.projects = projects
        self.votes: list[tuple[str, str]] = []
        self.vote_delay = 0.0

    async def get_by_open_id(self, open_id):
        return next((c for c in self.contestants if c.open_id == open_id), None)

    async def voted_project_ids(self, voter_record_id):
        return {p for v, p in self.votes if v == voter_record_id}

    async def add(self, voter_record_id, project_record_id):
        if self.vote_delay:
            await asyncio.sleep(self.vote_delay)
        self.votes.append((voter_record_id, project_record_id))


class FakeProjectRepo:
    def __init__(self, projects):
        self.projects = projects

    async def list_all(self):
        return self.projects

    async def get(self, record_id):
        return next((p for p in self.projects if p.record_id == record_id), None)

    async def incr_votes(self, record_id, delta=1):
        for p in self.projects:
            if p.record_id == record_id:
                p.votes += delta


class VoteDoubleSubmitTests(unittest.TestCase):
    """同一用户并发点击投票按钮：进程内锁保证只投一票（B3）。"""

    def test_concurrent_vote_actions_only_one_wins(self):
        from bot.core import service
        from bot.modules.vote import vote
        from bot.modules.vote.state import set_vote_open

        set_vote_open(True)
        me = Contestant(record_id="rec_me", open_id="ou_me", name="选手")
        project = Project(record_id="rec_p1", project_no="01", name="项目A")
        votes_repo = FakeVoteRepos([me], [project])
        votes_repo.vote_delay = 0.05  # 放大竞态窗口
        projects_repo = FakeProjectRepo([project])

        old_contestants, old_votes, old_projects = (service.SVC.contestants,
                                                    service.SVC.votes, service.SVC.projects)
        service.SVC.contestants = votes_repo
        service.SVC.votes = votes_repo
        service.SVC.projects = projects_repo
        try:
            async def click():
                ctx = types.SimpleNamespace(
                    open_id="ou_me", raw={"action": {"value": {"project_record_id": "rec_p1"}}})
                await vote.handle_vote_action(ctx)

            # 去重窗口外的两次并发回调：同一用户锁串行化，只成功一票。
            # patch 必须包住整个 asyncio.run 生命周期：协程在事件循环里恢复执行时，
            # click 内部的 with 块早已退出（asyncio.run 会新建循环）。
            async def run_two():
                await asyncio.gather(click(), click())

            with unittest.mock.patch("bot.modules.vote.vote.send_text",
                                     new_callable=unittest.mock.AsyncMock), \
                    unittest.mock.patch("bot.modules.vote.vote.send_card",
                                        new_callable=unittest.mock.AsyncMock):
                asyncio.run(run_two())
        finally:
            service.SVC.contestants = old_contestants
            service.SVC.votes = old_votes
            service.SVC.projects = old_projects

        # 两个并发请求里只有一个成功写入
        self.assertEqual(len(votes_repo.votes), 1)
        self.assertEqual(project.votes, 1)


class ActivityFlushTests(unittest.TestCase):
    """活跃度落库：落库成功才清零 delta；写失败保留缓冲（B3）。"""

    def test_flush_keeps_delta_when_write_fails(self):
        from bot.core import service
        from bot.modules.activity import activity as activity_module

        class FailingActivityRepo:
            async def today_rows(self, date):
                return []

            async def upsert_daily(self, contestant_record_id, date, delta):
                raise RuntimeError("feishu down")

            async def flush_totals(self, items):
                pass

        old_activity = service.SVC.activity
        service.SVC.activity = FailingActivityRepo()
        try:
            with activity_module._pending_lock:
                activity_module._pending["ou_x"] = {"record_id": "rec_x", "name": "X",
                                             "total": 3, "delta": 3}
            with self.assertRaises(RuntimeError):
                asyncio.run(activity_module.flush())
            # 写失败：delta 保留，等下一轮
            with activity_module._pending_lock:
                self.assertEqual(activity_module._pending["ou_x"]["delta"], 3)
        finally:
            service.SVC.activity = old_activity
            with activity_module._pending_lock:
                activity_module._pending.clear()

    def test_flush_clears_delta_after_success(self):
        from bot.core import service
        from bot.modules.activity import activity as activity_module

        class OkActivityRepo:
            def __init__(self):
                self.daily = []

            async def today_rows(self, date):
                return []

            async def upsert_daily(self, contestant_record_id, date, delta):
                self.daily.append((contestant_record_id, date, delta))

            async def flush_totals(self, items):
                pass

        repo = OkActivityRepo()
        old_activity = service.SVC.activity
        service.SVC.activity = repo
        try:
            with activity_module._pending_lock:
                activity_module._pending["ou_y"] = {"record_id": "rec_y", "name": "Y",
                                             "total": 5, "delta": 2}
            flushed = asyncio.run(activity_module.flush())
            self.assertEqual(flushed, 1)
            self.assertEqual(repo.daily, [("rec_y", unittest.mock.ANY, 2)])
            with activity_module._pending_lock:
                self.assertNotIn("ou_y", activity_module._pending)
        finally:
            service.SVC.activity = old_activity
            with activity_module._pending_lock:
                activity_module._pending.clear()


class EntityMappingTests(unittest.TestCase):
    """实体 <-> 飞书字段映射 round-trip（映射只存在于 adapters/feishu/repos.py）。"""

    def test_contestant_mapping(self):
        from bot.adapters.feishu import field_names as F
        from bot.adapters.feishu.repos import _to_contestant

        rec = {"record_id": "rec1", "fields": {
            F.C_NO: "WY01-0001", F.C_NAME: [{"text": "张三"}], F.C_PHONE: "13800138000",
            F.C_AUDIT: ["审核通过"], F.C_OPEN_ID: "ou_1", F.C_VERIFY: "已验证",
            F.C_MSG_COUNT: 12, F.C_SCORE: 30, F.C_INTENT: ["前端", "后端"],
        }}
        c = _to_contestant(rec)
        self.assertEqual(c.contestant_no, "WY01-0001")
        self.assertEqual(c.name, "张三")
        self.assertEqual(c.phone, "13800138000")
        self.assertEqual(c.audit_status, "审核通过")
        self.assertTrue(c.verified)
        self.assertTrue(c.approved)
        self.assertEqual(c.msg_count, 12)
        self.assertEqual(c.score, 30)
        self.assertEqual(c.intent_roles, ["前端", "后端"])

    def test_organizer_committee_identity(self):
        self.assertEqual(Organizer(identity="导师").committee_identity, "组委会-导师")
        self.assertEqual(Organizer().committee_identity, "组委会-主办方")


class CardDedupKeyTests(unittest.TestCase):
    """卡片去重键含完整按钮参数：同卡片不同项目按钮不互相吞，同一按钮重推仍幂等。"""

    def setUp(self):
        from bot.core import event_bus
        event_bus._seen_card_actions.clear()

    def _ctx(self, project_rid):
        return types.SimpleNamespace(
            open_id="ou_1", message_id="om_1", action_value="vote",
            raw={"action": {"value": {"action": "vote", "project_record_id": project_rid}}})

    def test_same_button_retransmission_is_duplicate(self):
        from bot.core.event_bus import _card_dedup
        self.assertFalse(_card_dedup(self._ctx("rec_p1")))
        self.assertTrue(_card_dedup(self._ctx("rec_p1")))

    def test_different_project_button_is_not_duplicate(self):
        from bot.core.event_bus import _card_dedup
        self.assertFalse(_card_dedup(self._ctx("rec_p1")))
        self.assertFalse(_card_dedup(self._ctx("rec_p2")))


class CardExpiryTests(unittest.TestCase):
    """验证卡片 15 分钟未操作自动过期；已提交的卡片不再过期；过期后迟到提交被拒。"""

    def setUp(self):
        from bot.modules.auth import auth
        auth._pending_cards.clear()

    def test_stale_card_is_expired_and_patched(self):
        from bot.modules.auth import auth
        auth._pending_cards["om_old"] = {"sent_at": time.time() - 16 * 60, "expired": False}
        with unittest.mock.patch.object(auth, "edit_card",
                                        new_callable=unittest.mock.AsyncMock) as edit:
            n = asyncio.run(auth._expire_stale_cards())
        self.assertEqual(n, 1)
        edit.assert_awaited_once()
        self.assertEqual(edit.await_args.args[0], "om_old")
        self.assertTrue(auth._pending_cards["om_old"]["expired"])

    def test_fresh_and_consumed_cards_are_not_patched(self):
        from bot.modules.auth import auth
        auth._track_card("om_new")
        auth._track_card("om_submitted")
        self.assertTrue(auth._consume_card("om_submitted"))
        with unittest.mock.patch.object(auth, "edit_card",
                                        new_callable=unittest.mock.AsyncMock) as edit:
            n = asyncio.run(auth._expire_stale_cards())
        self.assertEqual(n, 0)
        edit.assert_not_awaited()

    def test_late_submit_on_expired_card_is_rejected(self):
        from bot.modules.auth import auth
        auth._track_card("om_late")
        auth._pending_cards["om_late"]["expired"] = True
        self.assertFalse(auth._consume_card("om_late"))

    def test_untracked_card_submit_is_allowed(self):
        from bot.modules.auth import auth
        self.assertTrue(auth._consume_card("om_unknown"))


class FakeScoreRepos:
    """积分读改写竞态测试的假仓库：update 人为延迟放大竞态窗口。"""

    def __init__(self, contestant):
        self.contestant = contestant
        self.entries: list[tuple[int, int]] = []
        self.delay = 0.05

    async def get(self, record_id):
        return self.contestant

    async def update(self, record_id, **fields):
        await asyncio.sleep(self.delay)
        for k, v in fields.items():
            setattr(self.contestant, k, v)

    async def add(self, contestant_record_id, delta, reason, total_after):
        self.entries.append((delta, total_after))


class ScoreConcurrencyTests(unittest.TestCase):
    """同一选手并发加分：选手粒度锁保证总分不丢增量。"""

    def test_concurrent_add_score_no_lost_update(self):
        from bot.core import service
        from bot.modules.score.score import add_score

        c = Contestant(record_id="rec_s1", score=0)
        repo = FakeScoreRepos(c)
        old_contestants, old_scores = service.SVC.contestants, service.SVC.scores
        service.SVC.contestants = repo
        service.SVC.scores = repo
        try:
            async def run_two():
                await asyncio.gather(add_score("rec_s1", 5, "a"),
                                     add_score("rec_s1", 5, "b"))
            asyncio.run(run_two())
        finally:
            service.SVC.contestants = old_contestants
            service.SVC.scores = old_scores

        self.assertEqual(c.score, 10)
        self.assertEqual(sorted(t for _, t in repo.entries), [5, 10])


class FakeAuthContestantRepo:
    """验证竞态测试的假选手仓库：update 人为延迟放大竞态窗口。"""

    def __init__(self, contestants):
        self.contestants = contestants
        self.delay = 0.05
        self.bind_writes: list[str] = []

    async def get_by_open_id(self, open_id):
        return next((c for c in self.contestants if c.open_id == open_id), None)

    async def find_by_phone(self, phone):
        return next((c for c in self.contestants if c.phone == phone), None)

    async def update(self, record_id, **fields):
        await asyncio.sleep(self.delay)
        for c in self.contestants:
            if c.record_id == record_id:
                for k, v in fields.items():
                    setattr(c, k, v)
                if "open_id" in fields:
                    self.bind_writes.append(fields["open_id"])


class AuthDoubleClaimTests(unittest.TestCase):
    """两个账号并发提交同一手机号：手机号锁 + 占用检查保证只有一方绑定成功。"""

    def test_two_accounts_claim_same_phone_only_one_binds(self):
        from bot.core import service
        from bot.modules.auth import auth

        rec = Contestant(record_id="rec_c1", phone="13800138000", vx="abcd")
        repo = FakeAuthContestantRepo([rec])
        old_contestants = service.SVC.contestants
        service.SVC.contestants = repo
        try:
            def make_ctx(open_id):
                return types.SimpleNamespace(
                    open_id=open_id, message_id="", token=f"tk_{open_id}",
                    form_value={"phone": "13800138000", "vx": "abcd"})

            async def run_two():
                await asyncio.gather(auth.handle_verify_submit(make_ctx("ou_a")),
                                     auth.handle_verify_submit(make_ctx("ou_b")))

            with unittest.mock.patch.object(auth, "send_text",
                                            new_callable=unittest.mock.AsyncMock), \
                    unittest.mock.patch("bot.core.lark_client.update_card",
                                        new_callable=unittest.mock.AsyncMock):
                asyncio.run(run_two())
        finally:
            service.SVC.contestants = old_contestants

        self.assertIn(rec.open_id, ("ou_a", "ou_b"))
        self.assertEqual(len(repo.bind_writes), 1)


if __name__ == "__main__":
    unittest.main()
