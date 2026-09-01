"""普通用户侧能力装配入口（插件导出）。"""
from ..activity.activity import ActivityPlugin
from ..auth.auth import AuthPlugin
from ..profile.profile import ProfilePlugin
from ..score.score import ScorePlugin
from ..vote.vote import VotePlugin


__all__ = ["AuthPlugin", "ProfilePlugin", "ScorePlugin", "ActivityPlugin", "VotePlugin"]
