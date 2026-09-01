"""新架构单元测试：字段映射层、内存镜像、插件热插拔、写穿透并发安全。"""
import asyncio
import unittest
import unittest.mock

from bot.adapters.feishu import field_map
from bot.adapters.feishu.field_map import encode
from bot.adapters.feishu.mirror import Mirror
from bot.core.models import Contestant, Project, ScoreEntry
from bot.core.plugin import Plugin, PluginManager
from bot.core.registry import Registry


class FieldMapTests(unittest.TestCase):
    """写路径英->中映射：link 整形、未知键抛错（宁失败不静默丢写）。"""

    def test_encode_translates_and_shapes_links(self):
        out = encode(field_map.CONTESTANT, {"open_id": "ou_1", "verify_status": "已验证"})
        self.assertEqual(out, {"飞书open_id": "ou_1", "验证状态": "已验证"})

    def test_encode_link_cell(self):
        out = encode(field_map.VOTE, {"voter": ["rec_v"], "project": ["rec_p"]})
        self.assertEqual(out, {"投票人": [{"id": "rec_v"}], "项目": [{"id": "rec_p"}]})

    def test_encode_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            encode(field_map.CONTESTANT, {"no_such_key": "x"})

    def test_encode_empty_link(self):
        out = encode(field_map.TEAM, {"captain_ids": [], "member_ids": ["rec_m"]})
        self.assertEqual(out, {"队长": [], "队友": [{"id": "rec_m"}]})


class FakeStore:
    """镜像加载用的假 BaseStore：内存行 + API 调用计数。"""

    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables
        self.api_calls = 0

    def list_records(self, table: str) -> list[dict]:
        self.api_calls += 1
        return self.tables.get(table, [])

    def batch_create(self, table: str, rows: list[dict]) -> list[dict]:
        self.api_calls += 1
        out = []
        for i, r in enumerate(rows):
            rid = f"rec_new_{len(self.tables.get(table, []))}_{i}"
            self.tables.setdefault(table, []).append({"record_id": rid, "fields": r.get("fields", r)})
            out.append({"record_id": rid, "fields": r.get("fields", r)})
        return out

    def batch_update(self, table: str, items: list[dict]) -> None:
        self.api_calls += 1
        by_table = self.tables.setdefault(table, [])
        for it in items:
            for rec in by_table:
                if rec["record_id"] == it["record_id"]:
                    rec["fields"].update(it["fields"])


def _seed_store() -> FakeStore:
    return FakeStore({
        "选手表": [
            {"record_id": "rec_c1", "fields": {"选手ID": "WY01-0001", "姓名": "张三",
                                                "手机号": "13800138000", "飞书open_id": "ou_a",
                                                "验证状态": "已验证", "积分": 10, "发言数": 5}},
            {"record_id": "rec_c2", "fields": {"选手ID": "WY01-0002", "姓名": "李四",
                                                "手机号": "13900139000"}},
        ],
        "组委会表": [
            {"record_id": "rec_o1", "fields": {"姓名": "王五", "绑定码": "ZZB-TEST1234"}},
        ],
        "项目表": [
            {"record_id": "rec_p1", "fields": {"项目ID": "01", "项目名称": "项目A", "票数": 0}},
        ],
        "队伍表": [],
        "投票表": [],
        "积分表": [],
        "活跃度表": [],
    })


class MirrorTests(unittest.TestCase):
    """镜像：O(1) 读、写穿透后索引同步、聚合即时累加。"""

    def setUp(self):
        self.store = _seed_store()
        self.mirror = Mirror(self.store)
        self.mirror.load()

    def test_reads_are_o1_no_api(self):
        calls_before = self.store.api_calls
        self.assertIsNotNone(self.mirror.contestant_by_phone("13800138000"))
        self.assertIsNotNone(self.mirror.contestant_by_open_id("ou_a"))
        self.assertIsNotNone(self.mirror.organizer_by_code("ZZB-TEST1234"))
        self.assertEqual(self.mirror.project_by_rid("rec_p1").name, "项目A")
        self.assertEqual(self.store.api_calls, calls_before)

    def test_vote_add_updates_index_and_project_votes(self):
        self.mirror.apply_vote_add("rec_c1", "rec_p1")
        self.assertEqual(self.mirror.voted_project_ids("rec_c1"), {"rec_p1"})
        self.assertEqual(self.mirror.project_by_rid("rec_p1").votes, 1)

    def test_score_add_updates_contestant_score(self):
        self.mirror.apply_score_add(ScoreEntry(contestant_id="rec_c1", delta=5, total_after=15))
        self.assertEqual(self.mirror.contestant_by_rid("rec_c1").score, 15)
        self.assertEqual(len(self.mirror.score_history("rec_c1", 5)), 1)

    def test_contestant_update_reindexes_open_id(self):
        self.mirror.apply_contestant_update("rec_c2", {"open_id": "ou_b", "verify_status": "已验证"})
        self.assertIs(self.mirror.contestant_by_open_id("ou_b").record_id, "rec_c2")
        self.assertTrue(self.mirror.contestant_by_open_id("ou_b").verified)

    def test_activity_upsert_accumulates(self):
        self.mirror.apply_activity_upsert("rec_c1", "2026-09-01", 3)
        self.mirror.apply_activity_upsert("rec_c1", "2026-09-01", 2)
        self.assertEqual(self.mirror.activity_entry("rec_c1", "2026-09-01").msg_count, 5)


class WriteThroughTests(unittest.TestCase):
    """仓库写穿透：恰好 1 次 API、镜像同步、字段经映射层翻译。"""

    def test_vote_add_is_single_api_call(self):
        from bot.adapters.feishu.repos import FeishuVoteRepo

        store = _seed_store()
        mirror = Mirror(store)
        mirror.load()
        calls_after_load = store.api_calls
        repo = FeishuVoteRepo(mirror)

        asyncio.run(repo.add("rec_c1", "rec_p1"))

        self.assertEqual(store.api_calls - calls_after_load, 1)  # 写穿透恰好 1 次
        self.assertEqual(len(store.tables["投票表"]), 1)
        # 写入的是中文字段 + link 形态
        self.assertEqual(store.tables["投票表"][0]["fields"],
                         {"投票人": [{"id": "rec_c1"}], "项目": [{"id": "rec_p1"}]})
        self.assertEqual(mirror.voted_project_ids("rec_c1"), {"rec_p1"})
        self.assertEqual(mirror.project_by_rid("rec_p1").votes, 1)

    def test_contestant_update_translates_fields(self):
        from bot.adapters.feishu.repos import FeishuContestantRepo

        store = _seed_store()
        mirror = Mirror(store)
        mirror.load()
        repo = FeishuContestantRepo(mirror)

        asyncio.run(repo.update("rec_c2", open_id="ou_b", verify_status="已验证"))

        fields = store.tables["选手表"][1]["fields"]
        self.assertEqual(fields["飞书open_id"], "ou_b")
        self.assertEqual(fields["验证状态"], "已验证")
        self.assertEqual(mirror.contestant_by_open_id("ou_b").record_id, "rec_c2")

    def test_concurrent_votes_no_double_write(self):
        """同一用户并发投票：业务锁串行化，只写一票（镜像层端到端）。"""
        from bot.adapters.feishu.repos import (FeishuContestantRepo,
                                               FeishuProjectRepo, FeishuVoteRepo)

        store = _seed_store()
        mirror = Mirror(store)
        mirror.load()
        contestants = FeishuContestantRepo(mirror)
        votes = FeishuVoteRepo(mirror)
        projects = FeishuProjectRepo(mirror)

        async def scenario():
            lock = asyncio.Lock()
            writes = []

            async def vote_once():
                async with lock:  # 模拟 vote.py 的 _voter_lock
                    voted = await votes.voted_project_ids("rec_c1")
                    if "rec_p1" in voted:
                        return
                    await votes.add("rec_c1", "rec_p1")
                    writes.append("rec_p1")

            await asyncio.gather(vote_once(), vote_once())
            return writes

        writes = asyncio.run(scenario())
        self.assertEqual(writes, ["rec_p1"])
        self.assertEqual(len(store.tables["投票表"]), 1)
        self.assertEqual(mirror.project_by_rid("rec_p1").votes, 1)
        self.assertIsNotNone(projects)  # 装配完整性


class PluginFrameworkTests(unittest.TestCase):
    """插件框架：依赖排序、启停、注销、调度器联动。"""

    def setUp(self):
        self.registry = Registry()

    def _make(self, name, deps=(), jobs=()):
        p = Plugin(name=name, dependencies=deps)
        p.registry = self.registry  # 注入局部注册表，teardown 注销到它
        p.setup = lambda: [self.registry.job(f"{name}_job", 10, plugin=name)(lambda: None)
                           for _ in jobs]
        return p

    def test_dependency_order(self):
        pm = PluginManager()
        pm.register(self._make("admin", deps=("sync",)), self._make("sync"))
        order = pm._resolve_order(["admin", "sync"])
        self.assertEqual(order, ["sync", "admin"])

    def test_cycle_detection(self):
        pm = PluginManager()
        pm.register(self._make("a", deps=("b",)), self._make("b", deps=("a",)))
        with self.assertRaises(ValueError):
            pm._resolve_order(["a", "b"])

    def test_missing_dependency(self):
        pm = PluginManager()
        pm.register(self._make("a", deps=("ghost",)))
        with self.assertRaises(ValueError):
            pm._resolve_order(["a"])

    def test_disable_unregisters_and_unschedules(self):
        pm = PluginManager()
        pm.register(self._make("vote", jobs=("vote_job",)))
        scheduler = unittest.mock.Mock()
        scheduler.get_job.return_value = None
        pm.attach_scheduler(scheduler)

        pm.setup_all()
        self.assertEqual([j[0] for j in self.registry.jobs], ["vote_job"])

        pm.disable("vote")
        self.assertEqual([j[0] for j in self.registry.jobs], [])
        self.assertNotIn("vote", pm._enabled)

        pm.enable("vote")
        self.assertEqual([j[0] for j in self.registry.jobs], ["vote_job"])

    def test_unregister_plugin_removes_all_registrations(self):
        reg = Registry()

        async def handler(_ctx):
            pass

        reg.user_command("投票", plugin="vote")(handler)
        reg.on_card("vote", plugin="vote")(handler)
        reg.on_group_message(plugin="vote")(handler)
        reg.job("活跃度落库", 1, plugin="vote")(handler)
        reg.user_command("验证", plugin="auth")(handler)

        counts = reg.unregister_plugin("vote")
        self.assertEqual(counts, {"commands": 1, "cards": 1, "hooks": 1, "jobs": 1})
        self.assertNotIn("投票", reg.commands)
        self.assertNotIn("vote", reg.card_actions)
        self.assertEqual(reg.group_msg_hooks, [])
        self.assertEqual([j[0] for j in reg.jobs], [])
        self.assertIn("验证", reg.commands)  # 其他插件不受影响


class RealPluginAssemblyTests(unittest.TestCase):
    """真实插件装配：main.setup_plugins 的插件集可完整 setup/teardown。"""

    def test_all_plugins_setup_and_teardown(self):
        from bot.main import (ActivityPlugin, AdminJobsPlugin, AdminPlugin,
                              AuthPlugin, GroupPlugin, ProfilePlugin,
                              ScorePlugin, SyncPlugin, VotePlugin, PLUGIN_MANAGER)

        pm = PluginManager()
        for cls in (SyncPlugin, GroupPlugin, VotePlugin, ScorePlugin,
                    ActivityPlugin, AuthPlugin, ProfilePlugin,
                    AdminJobsPlugin, AdminPlugin):
            pm.register(cls())
        enabled = pm.setup_all()
        self.assertEqual(set(enabled), {"sync", "group", "vote", "score", "activity",
                                        "auth", "profile", "admin_jobs", "admin"})
        # 关键指令已注册
        from bot.core.registry import REGISTRY
        for cmd in ("验证", "投票", "票数", "查分", "活跃", "个人中心", "同步", "重载数据"):
            self.assertIn(cmd, REGISTRY.commands, cmd)
        # 全部停用后注册表清空（fallback 除外）
        for name in list(pm._enabled):
            pm.disable(name)
        self.assertEqual(REGISTRY.commands, {})
        self.assertEqual(REGISTRY.card_actions, {})
        self.assertEqual(REGISTRY.jobs, [])


if __name__ == "__main__":
    unittest.main()
