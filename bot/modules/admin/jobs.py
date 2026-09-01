"""管理员侧后台任务装配。"""
from __future__ import annotations

import logging

from ...core.config import CFG
from ...core.permissions import refresh_admins
from ...core.registry import REGISTRY
from ..group.group import (
    auto_backfill_job,
    promote_approved_job,
)
from ..sync.audience import sync_group_audience_options
from ..sync.sync import run_sync

log = logging.getLogger(__name__)
_registered = False


async def sync_job() -> None:
    await run_sync()
    await sync_group_audience_options()


async def admins_refresh_job() -> None:
    refresh_admins()


def register() -> None:
    """注册同步、权限刷新和群维护任务。"""
    global _registered
    if _registered:
        return
    REGISTRY.job("报名表自动同步", CFG.sync_interval_minutes)(sync_job)
    REGISTRY.job("管理员名单刷新", 10)(admins_refresh_job)
    REGISTRY.job("自动补拉", CFG.backfill_interval_minutes)(auto_backfill_job)
    REGISTRY.job("审核通过补拉", CFG.backfill_interval_minutes)(promote_approved_job)
    _registered = True
    log.debug("管理员侧后台任务已注册")


__all__ = ["register"]

