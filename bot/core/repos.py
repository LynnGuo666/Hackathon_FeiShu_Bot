"""Repository 接口：业务模块依赖这些 Protocol，不依赖任何具体存储实现。

- 全部方法为 ``async def``：飞书实现内部用 ``asyncio.to_thread`` 包装同步 SDK，
  未来 API 后端可原生 async，调用方代码不变。
- 方法语义按业务领域划分，参数/返回值只用 core.models 的实体和标量。
- 测试可按 duck-typing 提供假实现（参考 tests/test_modularity.py）。

未来接入自建 API：在 ``bot/adapters/api/`` 为每个 Protocol 写一个 HTTP 实现，
然后在 ``core.service.build_services`` 的 ``DATA_BACKEND=api`` 分支装配即可。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (ActivityEntry, Contestant, GroupConfig, Organizer,
                     Project, PullLog, Registration, ScoreEntry, Team,
                     VoteRecord)


@runtime_checkable
class ContestantRepo(Protocol):
    async def get_by_open_id(self, open_id: str) -> Contestant | None: ...
    async def get(self, record_id: str) -> Contestant | None: ...
    async def list_all(self) -> list[Contestant]: ...
    async def find_by_phone(self, phone: str) -> Contestant | None: ...
    async def update(self, record_id: str, **fields) -> None:
        """可更新字段：open_id / verify_status / msg_count / score / audit_status 等。"""
    async def batch_update(self, items: list[tuple[str, dict]]) -> None: ...
    async def create_many(self, rows: list[dict]) -> list[str]:
        """同步器批量新建选手，返回 record_id 列表。rows 为适配器约定的字段 dict。"""


@runtime_checkable
class OrganizerRepo(Protocol):
    async def get_by_open_id(self, open_id: str) -> Organizer | None: ...
    async def get_by_binding_code(self, code: str) -> Organizer | None: ...
    async def list_all(self) -> list[Organizer]: ...
    async def update(self, record_id: str, **fields) -> None:
        """可更新字段：open_id / verify_status / binding_code。"""
    async def batch_update(self, items: list[tuple[str, dict]]) -> None: ...


@runtime_checkable
class TeamRepo(Protocol):
    async def list_all(self) -> list[Team]: ...
    async def find_by_reg_record_id(self, reg_record_id: str) -> Team | None: ...
    async def create_many(self, rows: list[dict]) -> list[str]: ...
    async def batch_update(self, items: list[tuple[str, dict]]) -> None: ...


@runtime_checkable
class ProjectRepo(Protocol):
    async def list_all(self) -> list[Project]: ...
    async def get(self, record_id: str) -> Project | None: ...
    async def incr_votes(self, record_id: str, delta: int = 1) -> None:
        """兼容保留：票数权威值由投票表记录 + Base 公式字段聚合，实现方可为 no-op。"""


@runtime_checkable
class VoteRepo(Protocol):
    async def add(self, voter_record_id: str, project_record_id: str) -> None: ...
    async def voted_project_ids(self, voter_record_id: str) -> set[str]: ...


@runtime_checkable
class ScoreRepo(Protocol):
    async def add(self, contestant_record_id: str, delta: int, reason: str,
                  total_after: int) -> None:
        """写一条积分流水。total_after 由调用方（score 服务）计算后传入。"""
    async def history(self, contestant_record_id: str, limit: int = 5) -> list[ScoreEntry]: ...


@runtime_checkable
class ActivityRepo(Protocol):
    async def today_rows(self, date: str) -> list[ActivityEntry]: ...
    async def upsert_daily(self, contestant_record_id: str, date: str, delta: int) -> None:
        """按天 upsert 活跃度（write-behind，由 flush 任务批量调用）。"""


@runtime_checkable
class GroupRepo(Protocol):
    async def list_configs(self) -> list[GroupConfig]: ...
    async def create_config(self, cfg: GroupConfig) -> None: ...
    async def append_pull_log(self, logs: list[PullLog]) -> None: ...


@runtime_checkable
class RegistrationRepo(Protocol):
    async def list_all(self) -> list[Registration]: ...
