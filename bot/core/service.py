"""服务装配：按 DATA_BACKEND 构建仓库实现，业务模块经 SVC 服务定位器取用。

- DATA_BACKEND=feishu（默认）：飞书多维表格适配器（内存镜像 + 写穿透）；
- DATA_BACKEND=api：预留切换点，待自建 API 后端就绪后在 bot/adapters/api/ 实现同一批 Protocol。

SVC 是永不重新绑定的定位器对象：init_services() 原地填充属性，
模块在调用时才解析属性，因此测试可直接替换 SVC 的单个属性为假实现。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from .config import CFG
from .repos import (ActivityRepo, ContestantRepo, GroupRepo, OrganizerRepo,
                    ProjectRepo, RegistrationRepo, ScoreRepo, TeamRepo,
                    VoteRepo)

log = logging.getLogger(__name__)


class ServiceLocator:
    """属性在 init_services() 时填充；测试可直接赋值替换。"""

    contestants: Optional[ContestantRepo] = None
    organizers: Optional[OrganizerRepo] = None
    teams: Optional[TeamRepo] = None
    projects: Optional[ProjectRepo] = None
    votes: Optional[VoteRepo] = None
    scores: Optional[ScoreRepo] = None
    activity: Optional[ActivityRepo] = None
    groups: Optional[GroupRepo] = None
    registrations: Optional[RegistrationRepo] = None


SVC = ServiceLocator()

_REPO_ATTRS = ("contestants", "organizers", "teams", "projects", "votes",
               "scores", "activity", "groups", "registrations")


@dataclass
class Services:
    contestants: ContestantRepo
    organizers: OrganizerRepo
    teams: TeamRepo
    projects: ProjectRepo
    votes: VoteRepo
    scores: ScoreRepo
    activity: ActivityRepo
    groups: GroupRepo
    registrations: RegistrationRepo


class BaseStoreProxy:
    """惰性创建 BaseStore：避免 import service 模块时就要求凭证就绪。"""

    def __init__(self, base_token: str):
        self._base_token = base_token
        self._store = None

    def _get(self):
        if self._store is None:
            from .base_store import BaseStore
            self._store = BaseStore(self._base_token)
        return self._store

    def list_records(self, table: str) -> list[dict]:
        return self._get().list_records(table)

    def find(self, table: str, field: str, value: str) -> dict | None:
        return self._get().find(table, field, value)

    def batch_create(self, table: str, rows: list[dict]) -> list[dict]:
        return self._get().batch_create(table, rows)

    def batch_update(self, table: str, items: list[dict]) -> None:
        return self._get().batch_update(table, items)

    def resolve_table_id(self, table: str) -> str:
        return self._get().resolve_table_id(table)


def build_services() -> Services:
    backend = os.environ.get("DATA_BACKEND", "feishu").strip().lower()
    if backend == "feishu":
        from ..adapters.feishu.mirror import Mirror
        from ..adapters.feishu.repos import (FeishuActivityRepo,
                                             FeishuContestantRepo,
                                             FeishuGroupRepo,
                                             FeishuOrganizerRepo,
                                             FeishuProjectRepo,
                                             FeishuRegistrationRepo,
                                             FeishuScoreRepo, FeishuTeamRepo,
                                             FeishuVoteRepo)

        db = BaseStoreProxy(CFG.db_base_token)
        reg = BaseStoreProxy(CFG.reg_base_token)
        mirror = Mirror(db)
        contestants = FeishuContestantRepo(mirror)
        return Services(
            contestants=contestants,
            organizers=FeishuOrganizerRepo(mirror),
            teams=FeishuTeamRepo(mirror),
            projects=FeishuProjectRepo(mirror),
            votes=FeishuVoteRepo(mirror),
            scores=FeishuScoreRepo(mirror),
            activity=FeishuActivityRepo(mirror),
            groups=FeishuGroupRepo(db, CFG.tbl_group_config, CFG.tbl_pull_log),
            registrations=FeishuRegistrationRepo(reg, CFG.reg_table_id),
        )
    if backend == "api":
        raise NotImplementedError(
            "DATA_BACKEND=api 尚未接入：请在 bot/adapters/api/ 实现同一批 Repository Protocol 后在此装配。")
    raise ValueError(f"未知 DATA_BACKEND: {backend}（可选 feishu / api）")


def init_services() -> None:
    """进程启动时调用一次；重复调用幂等。"""
    if SVC.contestants is not None:
        return
    services = build_services()
    for attr in _REPO_ATTRS:
        setattr(SVC, attr, getattr(services, attr))


async def reload_data() -> dict:
    """强制全量刷新内存镜像（「重载数据」管理指令）。返回各表行数统计。"""
    from ..adapters.feishu.mirror import Mirror

    mirror = _find_mirror()
    if mirror is None:
        raise RuntimeError("当前数据后端没有内存镜像（DATA_BACKEND != feishu）")
    import asyncio
    await asyncio.to_thread(mirror.load)
    return {
        "选手": len(mirror.contestants_all()),
        "组委会": len(mirror.organizers_all()),
        "项目": len(mirror.projects_all()),
        "队伍": len(mirror.teams_all()),
    }


def _find_mirror() -> Optional[Mirror]:
    """从已装配的仓库里取镜像引用（feishu 后端时存在）。"""
    repo = SVC.contestants
    return getattr(repo, "_mirror", None)


from dataclasses import dataclass  # noqa: E402
