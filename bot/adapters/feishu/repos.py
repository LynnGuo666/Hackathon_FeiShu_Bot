"""飞书多维表格版 Repository 实现：实体 <-> 字段 dict 的映射只存在于此文件。

- 读路径全部走内存镜像（mirror.Mirror），O(1)，不再全表扫描（B1/万级并发核心）；
- 写路径经 field_map.encode 翻译为中文表字段后立即穿透（每次用户操作 1 次 API），
  成功后同步更新镜像；
- 聚合值（票数/积分总分/累计发言）不再读改写回表：由 Base 公式/查找引用字段
  服务端聚合，镜像内同步累加仅供即时展示；
- 业务唯一性锁（防双投/防重复绑定）保留在业务模块（vote.py / auth.py）。
"""
from __future__ import annotations

import asyncio

from ...core.models import (ActivityEntry, Contestant, GroupConfig, Organizer,
                            Project, PullLog, Registration, ScoreEntry, Team)
from . import cells
from . import field_map
from . import field_names as F
from .field_map import TableMap
from .mirror import Mirror

# 表名常量（选手数据库 Base 内）
T_CONTESTANTS = "选手表"
T_ORGANIZERS = "组委会表"
T_TEAMS = "队伍表"
T_PROJECTS = "项目表"
T_VOTES = "投票表"
T_SCORES = "积分表"
T_ACTIVITY = "活跃度表"


class _FeishuRepoBase:
    """注入镜像与表映射；写穿透统一走 _write_through。"""

    table: str = ""
    tmap: TableMap | None = None

    def __init__(self, mirror: Mirror, table: str, tmap: TableMap | None = None):
        self._mirror = mirror
        self._store = mirror.store  # 写穿透直接触达 BaseStore
        self.table = table
        self.tmap = tmap

    async def _write_through(self, table: str, items: list[tuple[str, dict]],
                             apply) -> None:
        """写穿透：API 写成功后应用镜像更新。items 为 (record_id, 业务键 fields)。"""
        if not items:
            return
        encoded = [(rid, field_map.encode(self.tmap, fields)) for rid, fields in items]
        await asyncio.to_thread(self._store_batch_update, table, encoded)
        for rid, fields in items:
            apply(rid, fields)

    def _store_batch_update(self, table: str, encoded_items: list[tuple[str, dict]]) -> None:
        self._store.batch_update(table, [{"record_id": rid, "fields": f}
                                         for rid, f in encoded_items])


# ---------- 选手 ----------

class FeishuContestantRepo(_FeishuRepoBase):
    tmap = field_map.CONTESTANT

    def __init__(self, mirror: Mirror):
        super().__init__(mirror, T_CONTESTANTS, self.tmap)

    async def get_by_open_id(self, open_id: str) -> Contestant | None:
        self._mirror.ensure_loaded()
        return self._mirror.contestant_by_open_id(open_id)

    async def get(self, record_id: str) -> Contestant | None:
        self._mirror.ensure_loaded()
        return self._mirror.contestant_by_rid(record_id)

    async def list_all(self) -> list[Contestant]:
        self._mirror.ensure_loaded()
        return self._mirror.contestants_all()

    async def find_by_phone(self, phone: str) -> Contestant | None:
        self._mirror.ensure_loaded()
        return self._mirror.contestant_by_phone(phone)

    async def update(self, record_id: str, **fields) -> None:
        await self.batch_update([(record_id, fields)])

    async def batch_update(self, items: list[tuple[str, dict]]) -> None:
        await self._write_through(self.table, items, self._mirror.apply_contestant_update)

    async def create_many(self, rows: list[dict]) -> list[str]:
        """同步器批量新建选手。rows 为业务键 dict；返回 record_id 列表。"""
        encoded = [field_map.encode(self.tmap, r) for r in rows]
        created = await asyncio.to_thread(self._store.batch_create, self.table, encoded)
        rids = [r.get("record_id", "") for r in created]
        for rid, row in zip(rids, rows):
            if rid:
                self._mirror.apply_contestant_create(rid, row)
        return rids


# ---------- 组委会 ----------

class FeishuOrganizerRepo(_FeishuRepoBase):
    tmap = field_map.ORGANIZER

    def __init__(self, mirror: Mirror):
        super().__init__(mirror, T_ORGANIZERS, self.tmap)

    async def get_by_open_id(self, open_id: str) -> Organizer | None:
        self._mirror.ensure_loaded()
        return self._mirror.organizer_by_open_id(open_id)

    async def get_by_binding_code(self, code: str) -> Organizer | None:
        self._mirror.ensure_loaded()
        return self._mirror.organizer_by_code(code)

    async def list_all(self) -> list[Organizer]:
        self._mirror.ensure_loaded()
        return self._mirror.organizers_all()

    async def update(self, record_id: str, **fields) -> None:
        await self.batch_update([(record_id, fields)])

    async def batch_update(self, items: list[tuple[str, dict]]) -> None:
        await self._write_through(self.table, items, self._mirror.apply_organizer_update)


# ---------- 队伍 ----------

class FeishuTeamRepo(_FeishuRepoBase):
    tmap = field_map.TEAM

    def __init__(self, mirror: Mirror):
        super().__init__(mirror, T_TEAMS, self.tmap)

    async def list_all(self) -> list[Team]:
        self._mirror.ensure_loaded()
        return self._mirror.teams_all()

    async def find_by_reg_record_id(self, reg_record_id: str) -> Team | None:
        for t in await self.list_all():
            if t.reg_record_id == reg_record_id:
                return t
        return None

    async def create_many(self, rows: list[dict]) -> list[str]:
        encoded = [field_map.encode(self.tmap, r) for r in rows]
        created = await asyncio.to_thread(self._store.batch_create, self.table, encoded)
        return [r.get("record_id", "") for r in created]

    async def batch_update(self, items: list[tuple[str, dict]]) -> None:
        encoded = [(rid, field_map.encode(self.tmap, fields)) for rid, fields in items]
        await asyncio.to_thread(self._store.batch_update, self.table,
                                [{"record_id": rid, "fields": f} for rid, f in encoded])


# ---------- 项目 ----------

class FeishuProjectRepo(_FeishuRepoBase):
    tmap = field_map.PROJECT

    def __init__(self, mirror: Mirror):
        super().__init__(mirror, T_PROJECTS, self.tmap)

    async def list_all(self) -> list[Project]:
        self._mirror.ensure_loaded()
        return self._mirror.projects_all()

    async def get(self, record_id: str) -> Project | None:
        self._mirror.ensure_loaded()
        return self._mirror.project_by_rid(record_id)

    async def incr_votes(self, record_id: str, delta: int = 1) -> None:
        """兼容保留：票数权威值由公式字段聚合，这里只更新镜像即时展示。

        正常投票路径走 FeishuVoteRepo.add（写投票表一条记录），不再调用本方法。
        """
        self._mirror.ensure_loaded()

    async def batch_update(self, items: list[tuple[str, dict]]) -> None:
        encoded = [(rid, field_map.encode(self.tmap, fields)) for rid, fields in items]
        await asyncio.to_thread(self._store.batch_update, self.table,
                                [{"record_id": rid, "fields": f} for rid, f in encoded])


# ---------- 投票 ----------

class FeishuVoteRepo(_FeishuRepoBase):
    tmap = field_map.VOTE

    def __init__(self, mirror: Mirror):
        super().__init__(mirror, T_VOTES, self.tmap)

    async def add(self, voter_record_id: str, project_record_id: str) -> None:
        """写一条投票记录（写穿透），成功后更新镜像（含项目票数即时 +1）。"""
        row = {"voter": [voter_record_id], "project": [project_record_id]}
        encoded = field_map.encode(self.tmap, row)
        await asyncio.to_thread(self._store.batch_create, self.table, [encoded])
        self._mirror.apply_vote_add(voter_record_id, project_record_id)

    async def voted_project_ids(self, voter_record_id: str) -> set[str]:
        self._mirror.ensure_loaded()
        return self._mirror.voted_project_ids(voter_record_id)


# ---------- 积分 ----------

class FeishuScoreRepo(_FeishuRepoBase):
    tmap = field_map.SCORE

    def __init__(self, mirror: Mirror):
        super().__init__(mirror, T_SCORES, self.tmap)

    async def add(self, contestant_record_id: str, delta: int, reason: str,
                  total_after: int) -> None:
        """写一条积分流水（写穿透），成功后更新镜像（含总分即时累加）。"""
        row = {"contestant": [contestant_record_id], "delta": delta,
               "total_after": total_after, "reason": reason}
        encoded = field_map.encode(self.tmap, row)
        await asyncio.to_thread(self._store.batch_create, self.table, [encoded])
        self._mirror.apply_score_add(ScoreEntry(
            record_id="", contestant_id=contestant_record_id,
            delta=delta, total_after=total_after, reason=reason))

    async def history(self, contestant_record_id: str, limit: int = 5) -> list[ScoreEntry]:
        self._mirror.ensure_loaded()
        return self._mirror.score_history(contestant_record_id, limit)


# ---------- 活跃度 ----------

class FeishuActivityRepo(_FeishuRepoBase):
    tmap = field_map.ACTIVITY

    def __init__(self, mirror: Mirror):
        super().__init__(mirror, T_ACTIVITY, self.tmap)

    async def today_rows(self, date: str) -> list[ActivityEntry]:
        self._mirror.ensure_loaded()
        return self._mirror.activity_today(date)

    async def upsert_daily(self, contestant_record_id: str, date: str, delta: int) -> None:
        """write-behind：活跃度按天 upsert。镜像已有记录则批量累加，否则新建。

        由 activity.flush() 批量调用（每分钟一轮），API 调用次数与活跃人数无关。
        """
        self._mirror.ensure_loaded()
        existing = self._mirror.activity_entry(contestant_record_id, date)
        if existing is None:
            row = {"contestant": [contestant_record_id], "date": date, "msg_count": delta}
            encoded = field_map.encode(self.tmap, row)
            created = await asyncio.to_thread(self._store.batch_create, self.table, [encoded])
            rid = (created[0].get("record_id", "") if created else "")
            self._mirror.apply_activity_upsert(contestant_record_id, date, delta, record_id=rid)
        else:
            fields = {"msg_count": existing.msg_count + delta}
            encoded = field_map.encode(self.tmap, fields)
            await asyncio.to_thread(self._store.batch_update, self.table,
                                    [{"record_id": existing.record_id, "fields": encoded}])
            self._mirror.apply_activity_upsert(contestant_record_id, date, delta)


# ---------- 群配置 / 拉群记录 ----------

class FeishuGroupRepo:
    """群配置与拉群记录：低频操作，直接走 store（不经镜像）。"""

    def __init__(self, store, config_table: str, log_table: str):
        self._store = store
        self._config_table = config_table
        self._log_table = log_table

    async def list_configs(self) -> list[GroupConfig]:
        recs = await asyncio.to_thread(self._store.list_records, self._config_table)
        out = []
        for r in recs:
            f = r.get("fields") or {}
            out.append(GroupConfig(
                record_id=r.get("record_id", ""),
                name=cells.text(f.get(F.G_NAME)),
                chat_id=cells.text(f.get(F.G_CHAT_ID)),
                enabled=cells.to_bool(f.get(F.G_ENABLED)),
                audiences=cells.select_many(f.get(F.G_AUDIENCES)),
                note=cells.text(f.get(F.G_NOTE)),
            ))
        return out

    async def create_config(self, cfg: GroupConfig) -> None:
        row = field_map.encode(field_map.GROUP_CONFIG, {
            "name": cfg.name, "chat_id": cfg.chat_id, "enabled": cfg.enabled,
            "audiences": cfg.audiences, "note": cfg.note})
        await asyncio.to_thread(self._store.batch_create, self._config_table, [row])

    async def append_pull_log(self, logs: list[PullLog]) -> None:
        rows = [field_map.encode(field_map.PULL_LOG, {
            "contestant": [l.contestant_id] if l.contestant_id else [],
            "chat_id": l.chat_id, "group_name": l.group_name,
            "result": l.result, "fail_reason": l.fail_reason}) for l in logs]
        await asyncio.to_thread(self._store.batch_create, self._log_table, rows)


# ---------- 报名表 ----------

class FeishuRegistrationRepo:
    """报名表：只读，字段保留原样（同步器专用），不经镜像。"""

    def __init__(self, store, table: str):
        self._store = store
        self.table = table

    async def list_all(self) -> list[Registration]:
        recs = await asyncio.to_thread(self._store.list_records, self.table)
        return [Registration(record_id=r.get("record_id", ""), fields=r.get("fields") or {})
                for r in recs]
