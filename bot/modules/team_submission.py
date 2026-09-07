"""组队登记导入的机器人调度接入。"""
from __future__ import annotations

from ..core.config import CFG
from ..core.plugin import Plugin
from ..core.registry import REGISTRY


async def process_team_submissions() -> None:
    """Apply eligible new submissions; the importer owns its status idempotency gate."""
    from scripts.import_team_submissions import run

    await run(apply=True)


class TeamSubmissionPlugin(Plugin):
    """Schedules automatic processing for newly submitted team registrations."""

    name = "team_submission"
    dependencies = ()

    def setup(self) -> None:
        REGISTRY.job(
            "组队登记自动判断",
            CFG.team_submission_interval_minutes,
            plugin=self.name,
        )(process_team_submissions)
