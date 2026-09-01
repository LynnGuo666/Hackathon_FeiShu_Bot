"""管理员侧后台任务插件（管理员名单刷新）。"""
from __future__ import annotations

import logging

from ...core.config import CFG
from ...core.permissions import refresh_admins
from ...core.plugin import Plugin
from ...core.registry import REGISTRY

log = logging.getLogger(__name__)


async def admins_refresh_job() -> None:
    refresh_admins()


class AdminJobsPlugin(Plugin):
    name = "admin_jobs"
    dependencies = ()

    def setup(self) -> None:
        REGISTRY.job("管理员名单刷新", 10, plugin=self.name)(admins_refresh_job)


__all__ = ["AdminJobsPlugin"]
