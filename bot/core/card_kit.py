"""卡片模板工具（Card 1.0 简单结构，够用且稳定）。"""
from __future__ import annotations


def result_card(title: str, ok: bool, lines: list[str]) -> dict:
    theme = "success" if ok else "danger"
    elements = [{
        "tag": "div",
        "text": {"tag": "lark_md", "content": "\n".join(lines)},
    }]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": theme},
        "elements": elements,
    }


def guide_card() -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "未央黑客松选手服务"}, "template": "blue"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "**可用指令**\n"
                "- `验证` —— 校验飞书账号手机号是否在已审核选手名单中，通过后自动拉入交流群\n"
                "- `活跃` —— 查看群发言活跃度排行\n"
                "- `投票` —— 决赛投票（一人一票）\n"
                "- `票数` —— 查看当前票榜\n"
                "- `同步` —— 手动触发一次报名表同步（管理员）\n"
                "- `补拉` —— 把已验证未入群的选手批量拉群（管理员）\n"
                "- `开票` / `关票` —— 开关投票通道（管理员）\n"
                "- `帮助` —— 查看本说明"}},
        ],
    }
