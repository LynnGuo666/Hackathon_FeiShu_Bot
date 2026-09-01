"""报名数据同步领域服务。

管理员指令和管理员名单不在这里注册；它们由 ``modules.admin`` 装配。
保留权限函数的导出只是为了兼容旧脚本和外部调用方，新的业务代码应从
``bot.core.permissions`` 导入。
"""
from ...core.permissions import (admins_status_line, is_admin, refresh_admins,
                                  test_recipient)
from .audience import sync_group_audience_options
from .sync import (audit_status_of, is_valid_phone, normalize_phone, run_sync)

__all__ = [
    "admins_status_line",
    "audit_status_of",
    "is_admin",
    "is_valid_phone",
    "normalize_phone",
    "refresh_admins",
    "run_sync",
    "sync_group_audience_options",
    "test_recipient",
]
