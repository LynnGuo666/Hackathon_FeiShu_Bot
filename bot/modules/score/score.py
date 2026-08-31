"""积分模块骨架：写积分流水到积分表。后续可扩展「加分/查分」指令。"""
from __future__ import annotations

from ...core.base_store import BaseStore
from ...core.config import CFG


def add_score(contestant_record_id: str, delta: int, reason: str, balance: int | None = None) -> None:
    store = BaseStore(CFG.db_base_token)
    fields: dict = {"选手": [{"id": contestant_record_id}], "变动分值": delta, "事由": reason}
    if balance is not None:
        fields["变动后积分"] = balance
    store.batch_create(CFG.tbl_score, [fields])
