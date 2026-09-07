"""飞书表格中文字段名常量：与 scripts/describe_fields.py 的 DESCRIPTIONS 逐项对应。

业务代码不允许出现中文字段名字符串，一律 import 这里的常量。
"""
from __future__ import annotations

# ---------- 选手表 ----------
C_NO = "选手ID"
C_NAME = "姓名"
C_PHONE = "手机号"
C_EMAIL = "邮箱"
C_VX = "vx号"
C_SCHOOL = "学校"
C_MAJOR = "专业"
C_GRADE = "年级"
C_IDENTITY = "身份"
C_INTENT = "意向角色"
C_AUDIT = "审核状态"
C_REG_RECORD = "报名记录ID"
C_OPEN_ID = "飞书open_id"
C_VERIFY = "验证状态"
C_MSG_COUNT = "发言数"
C_SCORE = "积分"

# ---------- 组委会表 ----------
O_NAME = "姓名"
O_PHONE = "手机号"
O_IDENTITY = "身份"
O_BINDING_CODE = "绑定码"
O_OPEN_ID = "飞书open_id"
O_VERIFY = "验证状态"

# ---------- 队伍表 ----------
T_NO = "队伍ID"
T_CAPTAIN = "队长"
T_MEMBERS = "队友"
T_MANUAL_MEMBERS = "手动队友"
T_PREFORMED = "是否预组队"
T_AGREE_ASSIGN = "同意统一分配"
T_REG_RECORD = "报名记录ID"

# ---------- 群配置表 ----------
G_NAME = "群名"
G_CHAT_ID = "chat_id"
G_ENABLED = "启用"
G_AUDIENCES = "面向身份"
G_NOTE = "备注"

# ---------- 拉群记录表 ----------
P_CONTESTANT = "选手"
P_CHAT_ID = "chat_id"
P_GROUP_NAME = "群名"
P_RESULT = "结果"
P_FAIL_REASON = "失败原因"

# ---------- 积分表 ----------
S_CONTESTANT = "选手"
S_DELTA = "变动分值"
S_TOTAL_AFTER = "变动后积分"
S_REASON = "事由"

# ---------- 项目表 ----------
J_NO = "项目ID"
J_NAME = "项目名称"
J_INTRO = "项目介绍"
J_TEAM = "队伍"
J_MEMBERS = "成员"
J_REPO = "仓库链接"
J_DEMO = "演示链接"
J_VOTES = "票数"

# ---------- 投票表 ----------
V_VOTER = "投票人"
V_PROJECT = "项目"

# ---------- 活跃度表 ----------
A_CONTESTANT = "选手"
A_DATE = "日期"
A_MSG_COUNT = "发言数"

# ---------- 报名表（同步器按角色前缀拼接） ----------
R_AUDIT = "审核状态"
R_PREFORMED = "是否预组队"
R_AGREE_ASSIGN = "是否同意统一分配"
