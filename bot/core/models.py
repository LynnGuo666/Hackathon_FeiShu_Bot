"""领域实体模型：业务模块只面向这些 dataclass，不接触飞书字段 dict。

实体字段用业务语义命名（英文），与飞书表格中文字段的映射只存在于
``bot/adapters/feishu/repos.py``。未来接入自建 API 后端时，只需为同一批
实体再写一个适配器实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Contestant:
    """选手（选手表一行）。"""
    record_id: str = ""
    contestant_no: str = ""        # 选手ID，WY01-XXXX
    name: str = ""
    phone: str = ""                # 归一化手机号（去重键）
    vx: str = ""
    school: str = ""
    major: str = ""
    grade: str = ""
    identity: str = ""
    intent_roles: list[str] = field(default_factory=list)
    audit_status: str = ""         # 审核状态：审核通过/未审核/审核不通过
    reg_record_id: str = ""        # 报名记录ID
    open_id: str = ""              # 飞书open_id（验证后绑定）
    verify_status: str = ""        # 验证状态：未验证/已验证
    msg_count: int = 0             # 发言数（累计）
    score: int = 0                 # 积分（总分）

    @property
    def verified(self) -> bool:
        return self.verify_status == "已验证"

    @property
    def approved(self) -> bool:
        return self.audit_status == "审核通过"


@dataclass
class Organizer:
    """组委会成员（组委会表一行）。"""
    record_id: str = ""
    name: str = ""
    phone: str = ""
    identity: str = ""             # 身份：导师/主办方/…
    binding_code: str = ""         # 绑定码
    open_id: str = ""
    verify_status: str = ""

    @property
    def verified(self) -> bool:
        return self.verify_status == "已验证"

    @property
    def committee_identity(self) -> str:
        """群身份：组委会-{身份}，身份为空视为主办方。"""
        return f"组委会-{self.identity or '主办方'}"


@dataclass
class Team:
    """队伍（队伍表一行）。"""
    record_id: str = ""
    team_no: str = ""              # 队伍ID，T-XXXXXXXX
    reg_record_id: str = ""        # 报名记录ID（upsert 键）
    preformed: str = ""            # 是否预组队
    agree_assign: str = ""         # 同意统一分配
    captain_ids: list[str] = field(default_factory=list)   # 队长（选手 record_id）
    member_ids: list[str] = field(default_factory=list)    # 队友（选手 record_id）


@dataclass
class Project:
    """参赛项目（项目表一行）。"""
    record_id: str = ""
    project_no: str = ""           # 项目ID
    name: str = ""                 # 项目名称
    intro: str = ""                # 项目介绍
    repo_url: str = ""             # 仓库链接
    demo_url: str = ""             # 演示链接
    votes: int = 0                 # 票数
    member_ids: list[str] = field(default_factory=list)    # 成员（选手 record_id）
    team_ids: list[str] = field(default_factory=list)      # 队伍（队伍 record_id）


@dataclass
class VoteRecord:
    """一条投票（投票表一行）。"""
    record_id: str = ""
    voter_id: str = ""             # 投票人（选手 record_id）
    project_id: str = ""           # 项目（项目 record_id）


@dataclass
class ScoreEntry:
    """一条积分流水（积分表一行）。"""
    record_id: str = ""
    contestant_id: str = ""        # 选手（选手 record_id）
    delta: int = 0                 # 变动分值
    total_after: int = 0           # 变动后积分
    reason: str = ""               # 事由


@dataclass
class ActivityEntry:
    """一条活跃度记录（活跃度表一行，一人一天一条）。"""
    record_id: str = ""
    contestant_id: str = ""        # 选手（选手 record_id）
    date: str = ""                 # 日期 YYYY-MM-DD
    msg_count: int = 0             # 当天发言数


@dataclass
class GroupConfig:
    """群配置（群配置表一行）。"""
    record_id: str = ""
    name: str = ""                 # 群名
    chat_id: str = ""
    enabled: bool = False          # 启用
    audiences: list[str] = field(default_factory=list)     # 面向身份（多选）
    note: str = ""                 # 备注


@dataclass
class PullLog:
    """一条拉群流水（拉群记录表）。"""
    contestant_id: str = ""        # 选手（选手 record_id，可空）
    chat_id: str = ""
    group_name: str = ""
    result: str = ""               # 成功/失败
    fail_reason: str = ""


@dataclass
class Registration:
    """一条报名记录（报名表一行，字段由同步器按角色展开）。"""
    record_id: str = ""
    fields: dict = field(default_factory=dict)   # 报名表字段保留原样（同步器专用）
