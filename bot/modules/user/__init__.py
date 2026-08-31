"""普通用户侧能力装配入口。"""
from ..activity import activity
from ..auth import auth
from ..profile import profile
from ..score import score
from ..vote import vote


_registered = False


def register() -> None:
    """注册全部用户指令、卡片回调和用户相关后台钩子。"""
    global _registered
    if _registered:
        return
    for module in (auth, profile, score, activity, vote):
        module.register()
    _registered = True


__all__ = ["register"]

