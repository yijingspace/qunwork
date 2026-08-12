"""组织级权限矩阵 (评估报告 增量3).

对齐《企业AI蜂群Agent组织架构全案》权限矩阵表,落地两个可操作的机制:

1. **角色 × 能力矩阵**: 每角色声明其能力(读写记忆/下发指令/项目组建/资金审批),
   `can(role, capability)` 校验 — 与 persona 连接器权限(工具级)互补,这是组织角色级。
2. **资金分级审批**: 按金额分三级(方案建议 <5k 总经理 / 5k-50w 董事长 / >50w
   董事会+人类终审),`fund_tier(amount)` 判定所需级别,`fund_approval(role, amount)`
   判定角色能否批准或需升级。

单机工具语义: 矩阵用于**标注组织合规级别 + 辅助人类审批决策**(inbox 审批即人类
终审兜底),不阻止人类 — 与方案五.3「人类兜底机制」一致。
"""

from __future__ import annotations

from typing import Optional

# -- 资金分级 (方案建议) ------------------------------------------------------
FUND_TIERS: list[tuple[float, str, str]] = [
    (5_000.0, "general_manager", "总经理级"),
    (500_000.0, "chairman", "董事长级"),
    (float("inf"), "board_human", "董事会合议 + 人类终审"),
]


def fund_tier(amount: float) -> dict:
    """The approval tier a payment of `amount` requires."""
    for limit, role, label in FUND_TIERS:
        if amount <= limit:
            return {"amount": amount, "limit": limit, "role": role, "label": label}
    raise ValueError("unreachable fund tier")  # pragma: no cover


# -- 角色 × 能力矩阵 ----------------------------------------------------------
# 能力标签:
#   read_memory / write_memory[:scope]     读写共享记忆(scope 细分: business/status/risk/…)
#   issue_commands[:scope]                 下发指令
#   fund:<tier>                             资金审批(≤5k=general_manager / ≤50w=chairman / board_human)
#   project_group                           项目组建
#   vote                                    集体决策投票
# 缺省: 未声明的能力 = 不允许(fail-closed)。
MATRIX: dict[str, set[str]] = {
    "chairman": {
        "read_memory", "write_memory", "issue_commands", "project_group",
        "fund:general_manager", "fund:chairman",
    },
    "board": {"read_memory", "write_memory:annotation", "vote", "fund:board_human"},
    "general_manager": {
        "read_memory", "write_memory", "issue_commands", "project_group",
        "fund:general_manager",
    },
    "operator": {
        "read_memory", "write_memory:status", "issue_commands:resource",
    },
    "reviewer": {"read_memory", "write_memory:report", "issue_commands:advise"},
    "worker": {"read_memory", "write_memory:business"},
    "compliance": {"read_memory", "write_memory:risk", "issue_commands:warn"},
    "risk": {"read_memory", "write_memory:risk_report", "issue_commands:alert"},
    "critic": {"read_memory", "write_memory:error", "issue_commands:reject"},
    "bus": {"read_memory", "write_memory:log", "issue_commands:route"},
    "memory_keeper": {"read_memory", "write_memory:archive"},
    "self_heal": {"write_memory:fault", "issue_commands:migrate"},
}

# 人类介入触发条件 (方案矩阵表「人类介入触发条件」列,精简)。
HUMAN_ESCALATION: dict[str, str] = {
    "chairman": "战略方向偏差、重大亏损、合规红线触碰",
    "board": "大额投融资、组织架构变更",
    "general_manager": "多项目资源冲突、任务大面积延期",
    "operator": "算力耗尽、Agent 大规模宕机",
    "reviewer": "连续项目失败、协同模式缺陷",
    "worker": "业务异常、数据矛盾、权限越界",
    "compliance": "违规操作、流程不合规",
    "risk": "资金/舆情/技术高风险",
    "critic": "Agent 幻觉、逻辑错误、输出失真",
    "bus": "消息风暴、通信阻塞",
    "memory_keeper": "存储溢出、索引失效",
    "self_heal": "Agent 实例崩溃、任务卡死",
}


def can(role: str, capability: str) -> bool:
    """Role may exercise `capability`? Capabilities not declared = denied."""
    return capability in MATRIX.get(role, set())


def fund_approval(role: str, amount: float) -> dict:
    """Can `role` approve a payment of `amount`? Returns the verdict + upgrade path."""
    tier = fund_tier(amount)
    allowed = can(role, f"fund:{tier['role']}")
    if allowed:
        return {
            "allowed": True,
            "role": role,
            "tier": tier["role"],
            "tier_label": tier["label"],
            "amount": amount,
        }
    # 升级路径: 当前角色无权 → 指向所需审批级别 + 人类介入条件。
    escalation = HUMAN_ESCALATION.get(tier["role"], HUMAN_ESCALATION.get(role, ""))
    return {
        "allowed": False,
        "role": role,
        "required_tier": tier["role"],
        "tier_label": tier["label"],
        "amount": amount,
        "escalation": escalation,
    }


def human_escalation_for(role: str) -> Optional[str]:
    return HUMAN_ESCALATION.get(role)


# -- 金额参数检测 (审批卡合规标注用) -------------------------------------------
_AMOUNT_KEYS = ("amount", "money", "price", "payment", "transfer", "金额", "数额")


def detect_amount(arguments: Optional[dict]) -> Optional[float]:
    """Find a monetary amount in a tool-call's arguments (for approval-card tier
    annotation). Returns the first parseable amount, or None."""
    if not isinstance(arguments, dict):
        return None
    for key, value in arguments.items():
        k = str(key).lower()
        if any(tok in k for tok in _AMOUNT_KEYS):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


# -- 工具 → 能力映射 (P2 增量: 审批合规标注) ----------------------------------
# 把工具名映射到组织能力标签,用于 can() 校验。
# 这不是安全边界(安全由 engine approver 保证)——这是合规标注,
# 告诉人类审批者「这个动作在组织矩阵里属于什么级别」。
_TOOL_CAPABILITY: dict[str, str] = {
    "write_file": "write_memory",
    "replace_in_file": "write_memory",
    "apply_patch": "write_memory",
    "apply_unified_diff": "write_memory",
    "run_shell": "issue_commands",
    "send_message": "issue_commands",
    "send_file": "issue_commands",
    "create_scheduled_task": "project_group",
}

# 资金相关工具 (需要额外金额分级检查)
_FUND_TOOLS: set[str] = {"send_message", "send_file", "run_shell"}


def annotate_compliance(
    tool_name: str,
    arguments: Optional[dict],
    member_role: Optional[str] = None,
) -> dict:
    """P2 核心: 给一个待审批的工具调用生成合规标注。

    返回字典包含:
    - tool_name: 工具名
    - capability: 对应的组织能力标签 (如 write_memory / issue_commands)
    - member_role: 当前成员角色 (如果已知)
    - role_has_capability: 当前角色是否具备该能力
    - fund_tier: 资金分级信息 (如果检测到金额)
    - fund_approval: 资金审批权限检查结果
    - escalation: 升级路径描述 (如果需要)
    - compliance_level: 综合合规级别 ("routine" / "elevated" / "board")

    这个标注是**辅助人类决策**的,不会自动批准或拒绝——与方案五.3「人类兜底机制」一致。
    """
    cap = _TOOL_CAPABILITY.get(tool_name)
    result: dict = {
        "tool_name": tool_name,
        "capability": cap,
        "member_role": member_role,
        "role_has_capability": None,
        "fund_tier": None,
        "fund_approval": None,
        "escalation": None,
        "compliance_level": "routine",
    }

    # 1) 角色能力检查
    if cap and member_role:
        result["role_has_capability"] = can(member_role, cap)

    # 2) 资金分级检查
    amount = detect_amount(arguments)
    if amount is not None and amount > 0:
        tier = fund_tier(amount)
        result["fund_tier"] = tier
        if member_role:
            approval = fund_approval(member_role, amount)
            result["fund_approval"] = approval
            if not approval["allowed"]:
                result["escalation"] = approval.get("escalation")
                result["compliance_level"] = "elevated" if tier["role"] != "board_human" else "board"
        else:
            # 没有角色信息时,仅标注级别
            result["compliance_level"] = "elevated" if tier["role"] != "board_human" else "board"

    # 3) 如果角色无能力且不是资金问题,标注为 elevated
    if result["role_has_capability"] is False and not amount:
        result["compliance_level"] = "elevated"
        result["escalation"] = human_escalation_for(member_role or "")

    return result
