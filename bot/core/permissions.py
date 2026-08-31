"""权限服务。

管理员身份来自飞书应用协作者，``ADMIN_OPEN_IDS`` 只作为 API 不可用时的
兜底配置。权限服务独立于具体业务模块，避免同步模块成为所有管理员逻辑的
依赖入口。
"""
from __future__ import annotations

import logging
import threading

from .config import CFG

log = logging.getLogger(__name__)


class AdminDirectory:
    """维护应用管理员集合，并提供线程安全的查询与刷新。"""

    def __init__(self) -> None:
        self._admins: set[str] = set(CFG.admin_open_ids)
        self._lock = threading.Lock()
        self._collaborators_failed = False

    @staticmethod
    def _fetch_collaborator_admins() -> set[str]:
        """读取应用协作者 open_id 集合。协作者都视为管理员。"""
        from lark_oapi.api.application.v6 import GetApplicationCollaboratorsRequest

        from .lark_client import client

        resp = client().application.v6.application_collaborators.get(
            GetApplicationCollaboratorsRequest.builder()
            .app_id(CFG.app_id)
            .user_id_type("open_id")
            .build())
        if not resp.success():
            raise RuntimeError(f"协作者 API 失败: {resp.code} {resp.msg}")
        return {c.user_id for c in (resp.data.collaborators or []) if c.user_id}

    def refresh(self) -> None:
        """刷新管理员名单，失败时保留可用名单并合并环境变量兜底。"""
        try:
            ids = self._fetch_collaborator_admins()
            with self._lock:
                self._admins = ids | set(CFG.admin_open_ids)
                self._collaborators_failed = False
            log.info("管理员名单已刷新: %d 人（应用协作者）", len(ids))
        except Exception as exc:
            with self._lock:
                self._admins.update(CFG.admin_open_ids)
                self._collaborators_failed = True
            log.warning(
                "获取应用协作者失败，管理员回落到 .env ADMIN_OPEN_IDS（%d 人）: %s",
                len(CFG.admin_open_ids),
                exc,
            )

    def is_admin(self, open_id: str) -> bool:
        with self._lock:
            return open_id in self._admins

    def status_line(self) -> str:
        with self._lock:
            if self._collaborators_failed and not CFG.admin_open_ids:
                return "⚠️ 管理员名单为空：协作者 API 不可用且未配置 ADMIN_OPEN_IDS，管理指令暂无人可用。"
            source = "应用协作者" if not self._collaborators_failed else ".env 兜底"
            return f"当前管理员 {len(self._admins)} 人（来源：{source}）。"

    def test_recipient(self) -> str:
        """选择测试消息收件人：环境变量优先，否则取应用协作者。"""
        if CFG.test_open_id:
            return CFG.test_open_id
        try:
            from lark_oapi.api.application.v6 import GetApplicationCollaboratorsRequest

            from .lark_client import client

            resp = client().application.v6.application_collaborators.get(
                GetApplicationCollaboratorsRequest.builder()
                .app_id(CFG.app_id)
                .user_id_type("open_id")
                .build())
            if resp.success():
                collaborators = [
                    (c.type, c.user_id)
                    for c in (resp.data.collaborators or [])
                    if c.user_id
                ]
                for wanted_type in ("administrator", "owner"):
                    for collaborator_type, open_id in collaborators:
                        if collaborator_type == wanted_type:
                            return open_id
                if collaborators:
                    return collaborators[0][1]
        except Exception:
            log.debug("查找测试消息收件人失败", exc_info=True)
        return ""


ADMIN_DIRECTORY = AdminDirectory()


def is_admin(open_id: str) -> bool:
    """返回 open_id 是否为管理员。"""
    return ADMIN_DIRECTORY.is_admin(open_id)


def refresh_admins() -> None:
    """刷新管理员名单。"""
    ADMIN_DIRECTORY.refresh()


def admins_status_line() -> str:
    """返回管理员名单状态描述。"""
    return ADMIN_DIRECTORY.status_line()


def test_recipient() -> str:
    """返回测试消息收件人。"""
    return ADMIN_DIRECTORY.test_recipient()

