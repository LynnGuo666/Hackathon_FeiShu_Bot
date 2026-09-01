"""组委会绑定码服务。

绑定码由管理员侧指令触发，但生成和持久化本身属于验证领域服务，因而不在
管理员命令处理器里直接操作表结构。

存储一律经 SVC 仓库接口（core.service），不直接接触飞书字段。
"""
from __future__ import annotations

from ...core.service import SVC


async def generate_binding_codes() -> list[dict[str, str]]:
    """为尚无绑定码的组委会记录生成绑定码并返回生成结果。"""
    organizers = await SVC.organizers.list_all()
    updates: list[tuple[str, dict]] = []
    generated: list[dict[str, str]] = []
    used: set[str] = set()

    for o in organizers:
        if o.binding_code:
            used.add(o.binding_code)
            continue

        code = _gen_bind_code()
        while code in used:
            code = _gen_bind_code()
        used.add(code)
        updates.append((o.record_id, {"binding_code": code}))
        generated.append({
            "name": o.name or "?",
            "identity": o.identity or "主办方",
            "code": code,
        })

    if updates:
        await SVC.organizers.batch_update(updates)
    return generated


def _gen_bind_code() -> str:
    """生成去掉易混淆字符的 ZZB-XXXXXXXX 绑定码。"""
    import secrets

    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "ZZB-" + "".join(secrets.choice(alphabet) for _ in range(8))
