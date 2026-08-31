"""管理员侧能力装配入口。"""
from .commands import register as register_commands
from .jobs import register as register_jobs


_registered = False


def register() -> None:
    """注册全部管理员指令和后台任务，保证重复调用不会重复注册。"""
    global _registered
    if _registered:
        return
    register_commands()
    register_jobs()
    _registered = True


__all__ = ["register"]

