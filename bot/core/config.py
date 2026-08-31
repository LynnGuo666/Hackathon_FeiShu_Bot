"""全局配置：从环境变量 / .env 读取。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


@dataclass
class Config:
    app_id: str = field(default_factory=lambda: _env("FEISHU_APP_ID"))
    app_secret: str = field(default_factory=lambda: _env("FEISHU_APP_SECRET"))

    reg_base_token: str = field(default_factory=lambda: _env("REG_BASE_TOKEN"))
    reg_table_id: str = field(default_factory=lambda: _env("REG_TABLE_ID"))

    db_base_token: str = field(default_factory=lambda: _env("DB_BASE_TOKEN"))
    tbl_contestants: str = field(default_factory=lambda: _env("TBL_CONTESTANTS", "选手表"))
    tbl_teams: str = field(default_factory=lambda: _env("TBL_TEAMS", "队伍表"))
    tbl_group_config: str = field(default_factory=lambda: _env("TBL_GROUP_CONFIG", "群配置表"))
    tbl_pull_log: str = field(default_factory=lambda: _env("TBL_PULL_LOG", "拉群记录表"))
    tbl_score: str = field(default_factory=lambda: _env("TBL_SCORE", "积分表"))
    tbl_activity: str = field(default_factory=lambda: _env("TBL_ACTIVITY", "活跃度表"))
    tbl_project: str = field(default_factory=lambda: _env("TBL_PROJECT", "项目表"))
    tbl_vote: str = field(default_factory=lambda: _env("TBL_VOTE", "投票表"))

    sync_interval_minutes: int = field(default_factory=lambda: int(_env("SYNC_INTERVAL_MINUTES", "10") or 10))
    admin_open_ids: list[str] = field(default_factory=lambda: [x for x in _env("ADMIN_OPEN_IDS").split(",") if x])

    @property
    def has_app_credentials(self) -> bool:
        return bool(self.app_id and self.app_secret)


CFG = Config()
