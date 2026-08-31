"""同步模块：注册「同步」指令与定时同步任务；管理员识别。"""
from __future__ import annotations

import logging
import threading
import time

from ...core.card_kit import result_card
from ...core.config import CFG
from ...core.lark_client import send_card, send_text
from ...core.registry import REGISTRY, MsgCtx
from .sync import run_sync

log = logging.getLogger(__name__)

# 管理员 = 飞书开发者后台「应用协作者」（owner / administrator 等），
# 启动时拉取并每 10 分钟刷新；.env ADMIN_OPEN_IDS 作为兜底（API 失败时仍可用）。
_admins: set[str] = set(CFG.admin_open_ids)
_admins_lock = threading.Lock()
_collaborators_failed = False  # 协作者 API 从未成功过时置 True，提示用户兜底配置


def _fetch_collaborator_admins() -> set[str]:
    """调应用协作者 API，返回协作者 open_id 集合（type 为 owner/administrator 等，全部算管理员）。"""
    from lark_oapi.api.application.v6 import GetApplicationCollaboratorsRequest

    from ...core.lark_client import client
    resp = client().application.v6.application_collaborators.get(
        GetApplicationCollaboratorsRequest.builder().app_id(CFG.app_id).user_id_type("open_id").build())
    if not resp.success():
        raise RuntimeError(f"协作者 API 失败: {resp.code} {resp.msg}")
    return {c.user_id for c in (resp.data.collaborators or []) if c.user_id}


def refresh_admins() -> None:
    """刷新管理员名单（协作者 API + .env 兜底合并）。"""
    global _collaborators_failed
    try:
        ids = _fetch_collaborator_admins()
        with _admins_lock:
            _admins.clear()
            _admins.update(ids)
            _admins.update(CFG.admin_open_ids)
        _collaborators_failed = False
        log.info("管理员名单已刷新: %d 人（应用协作者）", len(ids))
    except Exception as e:
        _collaborators_failed = True
        with _admins_lock:
            _admins.update(CFG.admin_open_ids)
        log.warning("获取应用协作者失败，管理员回落到 .env ADMIN_OPEN_IDS（%d 人）: %s",
                    len(CFG.admin_open_ids), e)


def is_admin(open_id: str) -> bool:
    return open_id in _admins


def admins_status_line() -> str:
    if _collaborators_failed and not CFG.admin_open_ids:
        return "⚠️ 管理员名单为空：协作者 API 不可用且未配置 ADMIN_OPEN_IDS，管理指令暂无人可用。"
    src = "应用协作者" if not _collaborators_failed else ".env 兜底"
    return f"当前管理员 {len(_admins)} 人（来源：{src}）。"


async def handle_sync(ctx: MsgCtx) -> None:
    await send_text(ctx.open_id, "开始同步报名表，请稍候…")
    stats = run_sync()
    await send_card(ctx.open_id, result_card(
        "同步完成", True,
        [f"- {k}：**{v}**" for k, v in stats.items()]))


async def handle_admins(ctx: MsgCtx) -> None:
    """「管理员」：查看当前管理员名单与来源。"""
    await send_card(ctx.open_id, result_card("管理员", True, [admins_status_line()]))


REGISTRY.command("同步")(handle_sync)
REGISTRY.command("管理员")(handle_admins)


@REGISTRY.job("报名表自动同步", CFG.sync_interval_minutes)
async def sync_job() -> None:
    run_sync()


@REGISTRY.job("管理员名单刷新", 10)
async def admins_refresh_job() -> None:
    refresh_admins()
