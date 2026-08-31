"""组委会绑定码服务。

绑定码由管理员侧指令触发，但生成和持久化本身属于验证领域服务，因而不在
管理员命令处理器里直接操作表结构。
"""
from __future__ import annotations

from ...core.base_store import BaseStore
from ...core.config import CFG


def generate_binding_codes(store: BaseStore | None = None) -> list[dict[str, str]]:
    """为尚无绑定码的组委会记录生成绑定码并返回生成结果。"""
    store = store or BaseStore(CFG.db_base_token)
    updates = []
    generated: list[dict[str, str]] = []
    used: set[str] = set()

    for record in store.list_records(CFG.tbl_organizers):
        fields = record.get("fields") or {}
        existing = _text(fields.get("绑定码"))
        if existing:
            used.add(existing)
            continue

        code = _gen_bind_code()
        while code in used:
            code = _gen_bind_code()
        used.add(code)
        updates.append({"record_id": record["record_id"], "fields": {"绑定码": code}})
        generated.append({
            "name": _text(fields.get("姓名")) or "?",
            "identity": _text(fields.get("身份")) or "主办方",
            "code": code,
        })

    if updates:
        store.batch_update(CFG.tbl_organizers, updates)
    return generated


def _gen_bind_code() -> str:
    """生成去掉易混淆字符的 ZZB-XXXXXXXX 绑定码。"""
    import secrets

    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "ZZB-" + "".join(secrets.choice(alphabet) for _ in range(8))


def _text(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in cell
        ).strip()
    return str(cell).strip()

