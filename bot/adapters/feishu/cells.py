"""bitable 单元格值归一化：全项目唯一的实现（此前在 6 个模块各复制一份）。

读回形态 -> 业务值：
- 文本字段：str 或 [{text:...}] 分段数组 -> str
- 单选：str 或 [选项] -> str
- 多选：[选项] -> [str]
- 关联（link）：[{"id": "rec..."}] 或 [{"record_ids": [...]}] -> [record_id]
"""
from __future__ import annotations


def text(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, list):
        parts = []
        for seg in cell:
            if isinstance(seg, dict):
                parts.append(str(seg.get("text", "")))
            else:
                parts.append(str(seg))
        return "".join(parts).strip()
    return str(cell).strip()


def select_one(cell) -> str:
    if isinstance(cell, list):
        return text(cell[0]) if cell else ""
    return text(cell)


def select_many(cell) -> list[str]:
    if isinstance(cell, list):
        return [text(x) for x in cell if text(x)]
    s = text(cell)
    return [s] if s else []


def link_ids(cell) -> list[str]:
    """link 字段读回形态是 [{"id": "rec..."}]（或 [{"record_ids": [...]}]），取出目标 record_id 列表。"""
    out: list[str] = []
    if not isinstance(cell, list):
        return out
    for x in cell:
        if not isinstance(x, dict):
            continue
        if x.get("id"):
            out.append(str(x["id"]))
        for rid in (x.get("record_ids") or []):
            if rid:
                out.append(str(rid))
    return out


def to_int(cell, default: int = 0) -> int:
    """数字字段读回可能是 int/float/str/文本段。"""
    if cell is None:
        return default
    if isinstance(cell, bool):
        return default
    if isinstance(cell, (int, float)):
        return int(cell)
    try:
        return int(text(cell))
    except (ValueError, TypeError):
        return default


def to_bool(cell) -> bool:
    return cell is True
