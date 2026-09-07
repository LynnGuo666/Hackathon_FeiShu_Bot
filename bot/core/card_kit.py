"""卡片模板工具（卡片 JSON 2.0，schema="2.0"）。

新版要点：
- 顶层 schema="2.0"，元素放 body.elements；仅支持 update_multi=true（共享卡片）。
- 按钮交互用 behaviors: [{type: "callback", value: {...}}]（没有 behaviors 不会触发回调）。
- 表单提交按钮用 form_action_type: "submit"，同样需配 behaviors，回调里表单值在 action.form_value。
- 回调事件仍是 card.action.trigger，value 取 action.value（与旧版一致）。
- 需要飞书客户端 7.20+，低版本只显示标题。
"""
from __future__ import annotations


def result_card(title: str, ok: bool, lines: list[str]) -> dict:
    theme = "green" if ok else "red"
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": theme},
        "body": {"elements": [{
            "tag": "markdown",
            "content": "\n".join(lines),
        }]},
    }


def guide_card(is_admin_user: bool = False) -> dict:
    """帮助卡片：快捷操作按钮 + 指令说明。管理员额外看到管理指令分区。"""
    def btn(text: str, action: str, style: str = "default") -> dict:
        return {"tag": "button", "text": {"tag": "plain_text", "content": text},
                "type": style, "size": "medium",
                "behaviors": [{"type": "callback", "value": {"action": action}}]}

    user_cmds = [
        "- `验证` —— 绑定飞书账号与报名信息，审核通过后自动拉入交流群",
        "- `个人中心` —— 我的选手 ID、队伍、项目提交状态",
        "- `活跃` —— 群发言活跃度排行（加「今天」看当日）",
        "- `投票` —— 决赛投票（一人一票）",
        "- `票数` —— 查看当前票榜",
        "- `查分` —— 查看我的积分与最近流水",
        "- `绑定 <码>` —— 用组委会绑定码额外绑定组委会身份（导师适用）",
        "- `帮助` —— 查看本说明",
    ]
    admin_cmds = [
        "- `同步` —— 手动触发报名表同步",
        "- `补拉` —— 把已验证用户补进匹配的群",
        "- `建群 <群名> [面向身份]` —— 机器人建外部群并登记",
        "- `加队友 <队伍编号> <手机号>` —— 手动把选手加入指定队伍（最多5人且一人一队）",
        "- `生成绑定码` —— 批量生成组委会绑定码",
        "- `开票` / `关票` —— 开关投票通道",
        "- `管理员` —— 查看管理员名单",
    ]
    elements = [
        {"tag": "markdown", "content": "**快捷操作**（点按钮即可，无需打字）"},
        {"tag": "column_set", "flex_mode": "bisect", "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                btn("✅ 验证身份", "verify", "primary")],
             "horizontal_align": "left"},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                btn("👤 个人中心", "profile")],
             "horizontal_align": "left"},
        ]},
        {"tag": "column_set", "flex_mode": "bisect", "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                btn("🗳️ 去投票", "vote")],
             "horizontal_align": "left"},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                btn("🔥 活跃度排行", "activity")],
             "horizontal_align": "left"},
        ]},
        {"tag": "column_set", "flex_mode": "bisect", "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                btn("📊 查看票榜", "votes_board")],
             "horizontal_align": "left"},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": []},
        ]},
        {"tag": "hr"},
        {"tag": "markdown", "content": "**全部指令**\n" + "\n".join(user_cmds)},
    ]
    if is_admin_user:
        elements += [
            {"tag": "hr"},
            {"tag": "markdown", "content": "**管理指令**（仅管理员可见）\n" + "\n".join(admin_cmds)},
        ]
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "未央黑客松选手服务"}, "template": "blue"},
        "body": {"elements": elements},
    }
