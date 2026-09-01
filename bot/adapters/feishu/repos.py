"""飞书多维表格版 Repository 实现：实体 <-> 字段 dict 的映射只存在于此文件。

- 所有同步 SDK 调用经 ``asyncio.to_thread`` 移出事件循环（B1）；
- 选手/组委会按 open_id 的查询带 60s TTL 进程内缓存（读多写少，砍掉大部分全表扫描），
  任何写操作后失效对应缓存；
- incr_votes / score 总分回写等读改写用进程内锁串行化（B3）。
"""
from __future__ import annotations

import asyncio
import threading
import time

from ...core.base_store import BaseStore
from ...core.models import (ActivityEntry, Contestant, GroupConfig, Organizer,
                            Project, PullLog, Registration, ScoreEntry, Team)
from . import field_names as F
from . import cells


def _link_cell(ids: list[str]) -> list[dict]:
    """业务侧 record_id 列表 -> bitable link 写入形态。"""
    return [{"id": rid} for rid in ids if rid]


# ---------- 实体映射（唯一出处） ----------

def _to_contestant(rec: dict) -> Contestant:
    f = rec.get("fields") or {}
    return Contestant(
        record_id=rec.get("record_id", ""),
        contestant_no=cells.text(f.get(F.C_NO)),
        name=cells.text(f.get(F.C_NAME)),
        phone=cells.text(f.get(F.C_PHONE)),
        vx=cells.text(f.get(F.C_VX)),
        school=cells.text(f.get(F.C_SCHOOL)),
        major=cells.text(f.get(F.C_MAJOR)),
        grade=cells.select_one(f.get(F.C_GRADE)),
        identity=cells.select_one(f.get(F.C_IDENTITY)),
        intent_roles=cells.select_many(f.get(F.C_INTENT)),
        audit_status=cells.select_one(f.get(F.C_AUDIT)),
        reg_record_id=cells.text(f.get(F.C_REG_RECORD)),
        open_id=cells.text(f.get(F.C_OPEN_ID)),
        verify_status=cells.select_one(f.get(F.C_VERIFY)),
        msg_count=cells.to_int(f.get(F.C_MSG_COUNT)),
        score=cells.to_int(f.get(F.C_SCORE)),
    )


def _to_organizer(rec: dict) -> Organizer:
    f = rec.get("fields") or {}
    return Organizer(
        record_id=rec.get("record_id", ""),
        name=cells.text(f.get(F.O_NAME)),
        phone=cells.text(f.get(F.O_PHONE)),
        identity=cells.select_one(f.get(F.O_IDENTITY)),
        binding_code=cells.text(f.get(F.O_BINDING_CODE)),
        open_id=cells.text(f.get(F.O_OPEN_ID)),
        verify_status=cells.select_one(f.get(F.O_VERIFY)),
    )


def _to_team(rec: dict) -> Team:
    f = rec.get("fields") or {}
    return Team(
        record_id=rec.get("record_id", ""),
        team_no=cells.text(f.get(F.T_NO)),
        reg_record_id=cells.text(f.get(F.T_REG_RECORD)),
        preformed=cells.select_one(f.get(F.T_PREFORMED)),
        agree_assign=cells.select_one(f.get(F.T_AGREE_ASSIGN)),
        captain_ids=cells.link_ids(f.get(F.T_CAPTAIN)),
        member_ids=cells.link_ids(f.get(F.T_MEMBERS)),
    )


def _to_project(rec: dict) -> Project:
    f = rec.get("fields") or {}
    return Project(
        record_id=rec.get("record_id", ""),
        project_no=cells.text(f.get(F.J_NO)),
        name=cells.text(f.get(F.J_NAME)),
        intro=cells.text(f.get(F.J_INTRO)),
        repo_url=cells.text(f.get(F.J_REPO)),
        demo_url=cells.text(f.get(F.J_DEMO)),
        votes=cells.to_int(f.get(F.J_VOTES)),
        member_ids=cells.link_ids(f.get(F.J_MEMBERS)),
        team_ids=cells.link_ids(f.get(F.J_TEAM)),
    )


def _to_score_entry(rec: dict) -> ScoreEntry:
    f = rec.get("fields") or {}
    return ScoreEntry(
        record_id=rec.get("record_id", ""),
        contestant_id=cells.link_ids(f.get(F.S_CONTESTANT))[0] if cells.link_ids(f.get(F.S_CONTESTANT)) else "",
        delta=cells.to_int(f.get(F.S_DELTA)),
        total_after=cells.to_int(f.get(F.S_TOTAL_AFTER)),
        reason=cells.text(f.get(F.S_REASON)),
    )


def _to_activity_entry(rec: dict) -> ActivityEntry:
    f = rec.get("fields") or {}
    links = cells.link_ids(f.get(F.A_CONTESTANT))
    return ActivityEntry(
        record_id=rec.get("record_id", ""),
        contestant_id=links[0] if links else "",
        date=cells.text(f.get(F.A_DATE)),
        msg_count=cells.to_int(f.get(F.A_MSG_COUNT)),
    )


def _to_group_config(rec: dict) -> GroupConfig:
    f = rec.get("fields") or {}
    return GroupConfig(
        record_id=rec.get("record_id", ""),
        name=cells.text(f.get(F.G_NAME)),
        chat_id=cells.text(f.get(F.G_CHAT_ID)),
        enabled=cells.to_bool(f.get(F.G_ENABLED)),
        audiences=cells.select_many(f.get(F.G_AUDIENCES)),
        note=cells.text(f.get(F.G_NOTE)),
    )


# ---------- 通用实现骨架 ----------

class _FeishuRepoBase:
    """注入 BaseStore 与表名；提供 to_thread 包装和 TTL 缓存工具。"""

    table: str = ""

    def __init__(self, store: BaseStore, table: str):
        self._store = store
        self.table = table

    def _list_sync(self) -> list[dict]:
        return self._store.list_records(self.table)


# ---------- 选手 ----------

class FeishuContestantRepo(_FeishuRepoBase):
    _CACHE_TTL = 60.0

    def __init__(self, store: BaseStore, table: str):
        super().__init__(store, table)
        self._lock = threading.Lock()
        self._by_open_id: dict[str, tuple[float, Contestant | None]] = {}
        self._score_locks: dict[str, threading.Lock] = {}

    def score_lock(self, record_id: str) -> threading.Lock:
        """按选手粒度的锁：积分总分读改写用（B3）。"""
        with self._lock:
            return self._score_locks.setdefault(record_id, threading.Lock())

    def _invalidate(self) -> None:
        with self._lock:
            self._by_open_id.clear()

    def _cached_get_by_open_id_sync(self, open_id: str) -> Contestant | None:
        now = time.monotonic()
        with self._lock:
            hit = self._by_open_id.get(open_id)
            if hit and now - hit[0] < self._CACHE_TTL:
                return hit[1]
        rec = self._store.find(self.table, F.C_OPEN_ID, open_id)
        ent = _to_contestant(rec) if rec else None
        with self._lock:
            self._by_open_id[open_id] = (now, ent)
        return ent

    async def get_by_open_id(self, open_id: str) -> Contestant | None:
        return await asyncio.to_thread(self._cached_get_by_open_id_sync, open_id)

    async def get(self, record_id: str) -> Contestant | None:
        for rec in await asyncio.to_thread(self._list_sync):
            if rec.get("record_id") == record_id:
                return _to_contestant(rec)
        return None

    async def list_all(self) -> list[Contestant]:
        recs = await asyncio.to_thread(self._list_sync)
        return [_to_contestant(r) for r in recs]

    async def find_by_phone(self, phone: str) -> Contestant | None:
        for c in await self.list_all():
            if c.phone == phone:
                return c
        return None

    async def update(self, record_id: str, **fields) -> None:
        await self.batch_update([(record_id, fields)])

    async def batch_update(self, items: list[tuple[str, dict]]) -> None:
        def _do():
            self._store.batch_update(self.table,
                                     [{"record_id": rid, "fields": f} for rid, f in items])
        await asyncio.to_thread(_do)
        self._invalidate()

    async def create_many(self, rows: list[dict]) -> list[str]:
        created = await asyncio.to_thread(self._store.batch_create, self.table, rows)
        self._invalidate()
        return [r.get("record_id", "") for r in created]


# ---------- 组委会 ----------

class FeishuOrganizerRepo(_FeishuRepoBase):
    _CACHE_TTL = 60.0

    def __init__(self, store: BaseStore, table: str):
        super().__init__(store, table)
        self._lock = threading.Lock()
        self._by_open_id: dict[str, tuple[float, Organizer | None]] = {}

    def _invalidate(self) -> None:
        with self._lock:
            self._by_open_id.clear()

    def _cached_get_by_open_id_sync(self, open_id: str) -> Organizer | None:
        now = time.monotonic()
        with self._lock:
            hit = self._by_open_id.get(open_id)
            if hit and now - hit[0] < self._CACHE_TTL:
                return hit[1]
        rec = self._store.find(self.table, F.O_OPEN_ID, open_id)
        ent = _to_organizer(rec) if rec else None
        with self._lock:
            self._by_open_id[open_id] = (now, ent)
        return ent

    async def get_by_open_id(self, open_id: str) -> Organizer | None:
        return await asyncio.to_thread(self._cached_get_by_open_id_sync, open_id)

    async def get_by_binding_code(self, code: str) -> Organizer | None:
        for o in await self.list_all():
            if o.binding_code == code:
                return o
        return None

    async def list_all(self) -> list[Organizer]:
        recs = await asyncio.to_thread(self._list_sync)
        return [_to_organizer(r) for r in recs]

    async def update(self, record_id: str, **fields) -> None:
        await self.batch_update([(record_id, fields)])

    async def batch_update(self, items: list[tuple[str, dict]]) -> None:
        def _do():
            self._store.batch_update(self.table,
                                     [{"record_id": rid, "fields": f} for rid, f in items])
        await asyncio.to_thread(_do)
        self._invalidate()


# ---------- 队伍 ----------

class FeishuTeamRepo(_FeishuRepoBase):
    async def list_all(self) -> list[Team]:
        recs = await asyncio.to_thread(self._list_sync)
        return [_to_team(r) for r in recs]

    async def find_by_reg_record_id(self, reg_record_id: str) -> Team | None:
        for t in await self.list_all():
            if t.reg_record_id == reg_record_id:
                return t
        return None

    async def create_many(self, rows: list[dict]) -> list[str]:
        created = await asyncio.to_thread(self._store.batch_create, self.table, rows)
        return [r.get("record_id", "") for r in created]

    async def batch_update(self, items: list[tuple[str, dict]]) -> None:
        await asyncio.to_thread(
            self._store.batch_update, self.table,
            [{"record_id": rid, "fields": f} for rid, f in items])


# ---------- 项目 ----------

class FeishuProjectRepo(_FeishuRepoBase):
    def __init__(self, store: BaseStore, table: str):
        super().__init__(store, table)
        self._vote_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def vote_lock(self, record_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._vote_locks.setdefault(record_id, threading.Lock())

    async def list_all(self) -> list[Project]:
        recs = await asyncio.to_thread(self._list_sync)
        return [_to_project(r) for r in recs]

    async def get(self, record_id: str) -> Project | None:
        for p in await self.list_all():
            if p.record_id == record_id:
                return p
        return None

    async def incr_votes(self, record_id: str, delta: int = 1) -> None:
        """读改写串行化：同项目并发 +1 不丢票（单实例内）。"""
        lock = self.vote_lock(record_id)

        def _do():
            with lock:
                rec = self._store.find(self.table, "record_id", record_id)
                if rec is None:
                    return
                cur = cells.to_int((rec.get("fields") or {}).get(F.J_VOTES))
                self._store.batch_update(self.table,
                                         [{"record_id": record_id, "fields": {F.J_VOTES: cur + delta}}])

        await asyncio.to_thread(_do)


# ---------- 投票 ----------

class FeishuVoteRepo(_FeishuRepoBase):
    async def add(self, voter_record_id: str, project_record_id: str) -> None:
        row = {F.V_VOTER: _link_cell([voter_record_id]),
               F.V_PROJECT: _link_cell([project_record_id])}
        await asyncio.to_thread(self._store.batch_create, self.table, [row])

    async def voted_project_ids(self, voter_record_id: str) -> set[str]:
        recs = await asyncio.to_thread(self._list_sync)
        voted: set[str] = set()
        for rec in recs:
            f = rec.get("fields") or {}
            if voter_record_id in cells.link_ids(f.get(F.V_VOTER)):
                voted.update(cells.link_ids(f.get(F.V_PROJECT)))
        return voted


# ---------- 积分 ----------

class FeishuScoreRepo(_FeishuRepoBase):
    async def add(self, contestant_record_id: str, delta: int, reason: str,
                  total_after: int) -> None:
        row = {F.S_CONTESTANT: _link_cell([contestant_record_id]),
               F.S_DELTA: delta, F.S_TOTAL_AFTER: total_after, F.S_REASON: reason}
        await asyncio.to_thread(self._store.batch_create, self.table, [row])

    async def history(self, contestant_record_id: str, limit: int = 5) -> list[ScoreEntry]:
        recs = await asyncio.to_thread(self._list_sync)
        entries = [_to_score_entry(r) for r in recs]
        mine = [e for e in entries if e.contestant_id == contestant_record_id]
        return mine[-limit:][::-1]


# ---------- 活跃度 ----------

class FeishuActivityRepo(_FeishuRepoBase):
    def __init__(self, store: BaseStore, contestants: FeishuContestantRepo, table: str):
        super().__init__(store, table)
        self._contestants = contestants

    async def today_rows(self, date: str) -> list[ActivityEntry]:
        recs = await asyncio.to_thread(self._list_sync)
        return [_to_activity_entry(r) for r in recs if _to_activity_entry(r).date == date]

    async def upsert_daily(self, contestant_record_id: str, date: str, delta: int) -> None:
        def _do():
            existing: dict[str, dict] = {}
            for rec in self._store.list_records(self.table):
                ent = _to_activity_entry(rec)
                if ent.date == date and ent.contestant_id:
                    existing[ent.contestant_id] = rec
            old = existing.get(contestant_record_id)
            if old:
                cur = cells.to_int((old.get("fields") or {}).get(F.A_MSG_COUNT))
                self._store.batch_update(self.table, [{
                    "record_id": old["record_id"], "fields": {F.A_MSG_COUNT: cur + delta}}])
            else:
                self._store.batch_create(self.table, [{
                    F.A_CONTESTANT: _link_cell([contestant_record_id]),
                    F.A_DATE: date, F.A_MSG_COUNT: delta}])

        await asyncio.to_thread(_do)

    async def flush_totals(self, items: list[tuple[str, int]]) -> None:
        """累计发言数回写选手表（带选手粒度锁，避免与积分并发回写互相覆盖）。"""
        def _do():
            updates = []
            for rid, total in items:
                with self._contestants.score_lock(rid):
                    rec = self._store.find(self._contestants.table, "record_id", rid)
                    if rec is None:
                        continue
                    cur = cells.to_int((rec.get("fields") or {}).get(F.C_MSG_COUNT))
                    updates.append({"record_id": rid, "fields": {F.C_MSG_COUNT: max(cur, total)}})
            if updates:
                self._store.batch_update(self._contestants.table, updates)

        await asyncio.to_thread(_do)


# ---------- 群配置 / 拉群记录 ----------

class FeishuGroupRepo(_FeishuRepoBase):
    def __init__(self, store: BaseStore, config_table: str, log_table: str):
        super().__init__(store, config_table)
        self._log_table = log_table

    async def list_configs(self) -> list[GroupConfig]:
        recs = await asyncio.to_thread(self._list_sync)
        return [_to_group_config(r) for r in recs]

    async def create_config(self, cfg: GroupConfig) -> None:
        row = {F.G_NAME: cfg.name, F.G_CHAT_ID: cfg.chat_id, F.G_ENABLED: cfg.enabled,
               F.G_AUDIENCES: cfg.audiences, F.G_NOTE: cfg.note}
        await asyncio.to_thread(self._store.batch_create, self.table, [row])

    async def append_pull_log(self, logs: list[PullLog]) -> None:
        rows = [{F.P_CONTESTANT: _link_cell([l.contestant_id]) if l.contestant_id else [],
                 F.P_CHAT_ID: l.chat_id, F.P_GROUP_NAME: l.group_name,
                 F.P_RESULT: l.result, F.P_FAIL_REASON: l.fail_reason} for l in logs]
        await asyncio.to_thread(self._store.batch_create, self._log_table, rows)


# ---------- 报名表 ----------

class FeishuRegistrationRepo(_FeishuRepoBase):
    async def list_all(self) -> list[Registration]:
        recs = await asyncio.to_thread(self._list_sync)
        return [Registration(record_id=r.get("record_id", ""), fields=r.get("fields") or {})
                for r in recs]
