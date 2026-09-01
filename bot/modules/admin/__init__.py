"""管理员侧能力装配入口（插件导出）。"""
from .commands import AdminPlugin
from .jobs import AdminJobsPlugin


__all__ = ["AdminPlugin", "AdminJobsPlugin"]
