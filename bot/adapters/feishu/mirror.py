"""内存镜像层：启动时全量加载各表建索引，读 O(1)，写穿透后同步更新镜像。

设计（单实例部署）：
- 读路径（find_by_phone / get_by_open_id / get / voted_project_ids / today_rows /
  history）全部走内存，不再触发全表扫描 API——这是万级并发的核心改造。
- 写穿透：验证绑定、投票、积分流水每次用户操作恰好 1 次 API 调用，成功后更新镜像。
- 活跃度 write-behind：内存 diff，flush 任务批量落库（每轮几次 API 调用）。
- 线程安全：所有可变索引由 threading.Lock 保护（读多写少，锁竞争可忽略）；
  事件循环线程与 to_thread 线程池都可能访问。
- 「重载数据」管理指令可强制全量刷新，兜底 Base 手工改数据的场景。
"""
from __future__ import annotations

import logging
import threading

from ...core.models import (ActivityEntry, Contestant, Organizer, Project,
                            ScoreEntry, Team)
from . import cells
from . import field_names as F

log = logging.getLogger(__name__)


class Mirror:
    """选手数据库 Base 的内存镜像（选手/组委会/项目/投票/积分/活跃度/队伍）。"""

    def __init__(self, store):
        self._store = store
        self._lock = threading.RLock()
        self._loaded = False        # 选手
        self._contestants_by_rid: dict[str, Contestant] = {}
        self._contestants_by_phone: dict[str, Contestant] = {}
        self._contestants_by_open_id: dict[str, Contestant] = {}
        # 组委会
        self._organizers_by_rid: dict[str, Organizer] = {}
        self._organizers_by_open_id: dict[str, Organizer] = {}
        self._organizers_by_code: dict[str, Organizer] = {}
        # 项目 / 队伍
        self._projects_by_rid: dict[str, Project] = {}
        self._teams_by_rid: dict[str, Team] = {}
        # 投票：voter_record_id -> {project_record_id}
        self._votes_by_voter: dict[str, set[str]] = {}
        # 积分流水：contestant_record_id -> [ScoreEntry]（追加序）
        self._scores_by_contestant: dict[str, list[ScoreEntry]] = {}
        # 活跃度：(contestant_record_id, date) -> ActivityEntry
        self._activity_by_key: dict[tuple[str, str], ActivityEntry] = {}

    # ---------- 加载 ----------

    @property
    def store(self):
        """底层 BaseStore 引用（repo 写穿透用）。"""
        return self._store

    def load(self) -> None:
        """全量加载各表并建索引（启动时 / 「重载数据」时调用）。"""
        with self._lock:
            contestants: dict[str, Contestant] = {}
            for rec in self._store.list_records("选手表"):
                c = _to_contestant(rec)
                contestants[c.record_id] = c
            self._contestants_by_rid = contestants
            self._contestants_by_phone = {c.phone: c for c in contestants.values() if c.phone}
            self._contestants_by_open_id = {c.open_id: c for c in contestants.values() if c.open_id}

            organizers: dict[str, Organizer] = {}
            for rec in self._store.list_records("组委会表"):
                o = _to_organizer(rec)
                organizers[o.record_id] = o
            self._organizers_by_rid = organizers
            self._organizers_by_open_id = {o.open_id: o for o in organizers.values() if o.open_id}
            self._organizers_by_code = {o.binding_code: o for o in organizers.values() if o.binding_code}

            self._projects_by_rid = {}
            for rec in self._store.list_records("项目表"):
                p = _to_project(rec)
                self._projects_by_rid[p.record_id] = p

            self._teams_by_rid = {}
            for rec in self._store.list_records("队伍表"):
                t = _to_team(rec)
                self._teams_by_rid[t.record_id] = t

            votes: dict[str, set[str]] = {}
            for rec in self._store.list_records("投票表"):
                f = rec.get("fields") or {}
                for voter in cells.link_ids(f.get(F.V_VOTER)):
                    votes.setdefault(voter, set()).update(cells.link_ids(f.get(F.V_PROJECT)))
            self._votes_by_voter = votes

            scores: dict[str, list[ScoreEntry]] = {}
            for rec in self._store.list_records("积分表"):
                e = _to_score_entry(rec)
                if e.contestant_id:
                    scores.setdefault(e.contestant_id, []).append(e)
            self._scores_by_contestant = scores

            activity: dict[tuple[str, str], ActivityEntry] = {}
            for rec in self._store.list_records("活跃度表"):
                e = _to_activity_entry(rec)
                if e.contestant_id and e.date:
                    activity[(e.contestant_id, e.date)] = e
            self._activity_by_key = activity

            self._loaded = True
        log.info("镜像加载完成: 选手 %d, 组委会 %d, 项目 %d, 队伍 %d, 投票 %d 人, 积分 %d 人, 活跃度 %d 条",
                 len(self._contestants_by_rid), len(self._organizers_by_rid),
                 len(self._projects_by_rid), len(self._teams_by_rid),
                 len(self._votes_by_voter), len(self._scores_by_contestant),
                 len(self._activity_by_key))

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ---------- 读（O(1)） ----------

    def contestant_by_rid(self, rid: str) -> Contestant | None:
        return self._contestants_by_rid.get(rid)

    def contestant_by_phone(self, phone: str) -> Contestant | None:
        return self._contestants_by_phone.get(phone)

    def contestant_by_open_id(self, open_id: str) -> Contestant | None:
        return self._contestants_by_open_id.get(open_id)

    def contestants_all(self) -> list[Contestant]:
        return list(self._contestants_by_rid.values())

    def organizer_by_rid(self, rid: str) -> Organizer | None:
        return self._organizers_by_rid.get(rid)

    def organizer_by_open_id(self, open_id: str) -> Organizer | None:
        return self._organizers_by_open_id.get(open_id)

    def organizer_by_code(self, code: str) -> Organizer | None:
        return self._organizers_by_code.get(code)

    def organizers_all(self) -> list[Organizer]:
        return list(self._organizers_by_rid.values())

    def project_by_rid(self, rid: str) -> Project | None:
        return self._projects_by_rid.get(rid)

    def projects_all(self) -> list[Project]:
        return list(self._projects_by_rid.values())

    def teams_all(self) -> list[Team]:
        return list(self._teams_by_rid.values())

    def voted_project_ids(self, voter_rid: str) -> set[str]:
        return set(self._votes_by_voter.get(voter_rid, ()))

    def score_history(self, contestant_rid: str, limit: int = 5) -> list[ScoreEntry]:
        entries = self._scores_by_contestant.get(contestant_rid, [])
        return entries[-limit:][::-1]

    def activity_entry(self, contestant_rid: str, date: str) -> ActivityEntry | None:
        return self._activity_by_key.get((contestant_rid, date))

    def activity_today(self, date: str) -> list[ActivityEntry]:
        return [e for (rid, d), e in self._activity_by_key.items() if d == date]

    # ---------- 写穿透后镜像更新（调用方保证 API 写已成功） ----------

    def apply_contestant_update(self, rid: str, fields: dict) -> None:
        """fields 为业务英文键 dict（与写路径一致）。"""
        with self._lock:
            c = self._contestants_by_rid.get(rid)
            if c is None:
                return
            for key, value in fields.items():
                setattr(c, key, value)
            # 索引维护：phone / open_id 可能变化
            self._contestants_by_phone.pop(c.phone, None)
            if c.phone:
                self._contestants_by_phone[c.phone] = c
            self._contestants_by_open_id.pop(c.open_id, None)
            if c.open_id:
                self._contestants_by_open_id[c.open_id] = c

    def apply_contestant_create(self, rid: str, fields: dict) -> None:
        with self._lock:
            c = Contestant(record_id=rid, **fields)
            self._contestants_by_rid[rid] = c
            if c.phone:
                self._contestants_by_phone[c.phone] = c
            if c.open_id:
                self._contestants_by_open_id[c.open_id] = c

    def apply_organizer_update(self, rid: str, fields: dict) -> None:
        with self._lock:
            o = self._organizers_by_rid.get(rid)
            if o is None:
                return
            for key, value in fields.items():
                setattr(o, key, value)
            self._organizers_by_open_id.pop(o.open_id, None)
            if o.open_id:
                self._organizers_by_open_id[o.open_id] = o
            self._organizers_by_code.pop(o.binding_code, None)
            if o.binding_code:
                self._organizers_by_code[o.binding_code] = o

    def apply_vote_add(self, voter_rid: str, project_rid: str) -> None:
        with self._lock:
            self._votes_by_voter.setdefault(voter_rid, set()).add(project_rid)
            p = self._projects_by_rid.get(project_rid)
            if p is not None:
                p.votes += 1  # 即时展示用；权威值由公式字段聚合

    def apply_score_add(self, entry: ScoreEntry) -> None:
        with self._lock:
            if entry.contestant_id:
                self._scores_by_contestant.setdefault(entry.contestant_id, []).append(entry)
            c = self._contestants_by_rid.get(entry.contestant_id)
            if c is not None:
                c.score += entry.delta  # 即时展示用；权威值由公式字段聚合

    def apply_activity_upsert(self, contestant_rid: str, date: str, delta: int,
                              record_id: str | None = None) -> None:
        """delta 为本次落库增量；record_id 为新建记录的 id（write-behind 回填）。"""
        with self._lock:
            key = (contestant_rid, date)
            e = self._activity_by_key.get(key)
            if e is None:
                self._activity_by_key[key] = ActivityEntry(
                    record_id=record_id or "", contestant_id=contestant_rid,
                    date=date, msg_count=delta)
            else:
                e.msg_count += delta
                if record_id:
                    e.record_id = record_id

    def apply_activity_create_result(self, contestant_rid: str, date: str,
                                     record_id: str) -> None:
        with self._lock:
            e = self._activity_by_key.get((contestant_rid, date))
            if e is not None and not e.record_id:
                e.record_id = record_id


# ---------- 实体映射（读路径：中文 -> 英文；与 repos.py 共用，迁至此处） ----------

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
    links = cells.link_ids(f.get(F.S_CONTESTANT))
    return ScoreEntry(
        record_id=rec.get("record_id", ""),
        contestant_id=links[0] if links else "",
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
