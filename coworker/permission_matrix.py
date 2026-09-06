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

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

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
    # 2026-09-06 角色统一 (方案A): 成员页可设置的 scheduler/auditor 曾不在矩阵,
    # can() fail-closed 全禁 (连 read_memory 都无) — 补齐。
    "scheduler": {
        "read_memory", "write_memory:log", "issue_commands:route", "project_group",
    },
    "auditor": {"read_memory", "write_memory:report", "issue_commands:warn"},
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

# 角色别名 (历史数据迁移用): 旧成员行 role='gm' → 规范名 general_manager。
_ROLE_ALIASES: dict[str, str] = {"gm": "general_manager"}


def normalize_role(role: str) -> str:
    """Map a historical role alias to its canonical name (unknown → unchanged)."""
    return _ROLE_ALIASES.get(str(role), str(role))


# -- 角色注册表 (2026-09-06 方案A: 单一角色定义源) -----------------------------
# team 校验 / 权限矩阵 / GUI 下拉三方共用, 不再各自硬编码词汇表。
ROLE_REGISTRY: dict[str, dict] = {
    "chairman": {
        "label": "董事长", "description": "战略决策与最高资金审批",
    },
    "board": {
        "label": "董事会", "description": "集体决策投票与超大额资金终审",
    },
    "general_manager": {
        "label": "总经理", "description": "日常经营决策、项目组建与 ≤5k 资金审批",
    },
    "scheduler": {
        "label": "运营调度", "description": "任务派单、资源路由与蜂群组建",
    },
    "reviewer": {
        "label": "审校", "description": "评审产出、撰写报告、建议性指令",
    },
    "auditor": {
        "label": "合规审计", "description": "只读审计全库、出具审计报告与合规警示",
    },
    "worker": {
        "label": "工蜂", "description": "执行具体业务并沉淀业务记忆",
    },
    "compliance": {
        "label": "风控合规", "description": "违规检测与流程合规警示",
    },
    "risk": {
        "label": "风险评估", "description": "资金/舆情/技术风险预警",
    },
    "critic": {
        "label": "纠错批判", "description": "识别幻觉与逻辑错误、驳回失真输出",
    },
    "operator": {
        "label": "运维操作", "description": "算力资源管理与状态更新",
    },
    "bus": {
        "label": "消息总线", "description": "组织内消息路由与通信日志",
    },
    "memory_keeper": {
        "label": "记忆管家", "description": "共享记忆归档与索引维护",
    },
    "self_heal": {
        "label": "自愈代理", "description": "故障记录与实例迁移恢复",
    },
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
    """Role may exercise `capability`? Capabilities not declared = denied.
    历史别名 (gm) 自动规范化 — 存量数据迁移前后的行为一致。"""
    return capability in MATRIX.get(normalize_role(role), set())


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


# -- 方案B: 角色即能力包 — 运行时组织门禁 (2026-09-06) -------------------------
# 语义: 组织角色约束是**叠加**在现有审批兜底 (approver/PermissionEngine) 之上,
# 不是替代 — 未映射工具不受 gate (读/搜索等操作照常), 已映射工具按矩阵硬拒,
# 资金动作按分级审批硬拒 (超出角色权限 → DENY + 升级路径)。persona 声明
# org_role 即受约束; 无 org_role / 未知角色 = 不 gate (现状行为)。

def role_can_tool(role: str, tool_name: str) -> bool:
    """组织角色可否调用该工具。未映射工具返回 True (不受组织约束)。
    scope 变体视为持有该能力 (write_memory:business → 可写, 具体 scope 粒度
    由 annotate_compliance 在审批卡标注; 门禁先按能力级粗粒度执行)。"""
    cap = _TOOL_CAPABILITY.get(tool_name)
    if cap is None:
        return True
    caps = MATRIX.get(normalize_role(role), set())
    return cap in caps or any(c.startswith(f"{cap}:") for c in caps)


def org_gate(role: str, tool_name: str, arguments: Optional[dict]) -> tuple[bool, str]:
    """运行时组织门禁: 角色 + 工具 (+ 金额) → (allowed, deny_reason)。"""
    norm = normalize_role(str(role or ""))
    if not norm or norm not in MATRIX:
        return True, ""
    if not role_can_tool(norm, tool_name):
        cap = _TOOL_CAPABILITY.get(tool_name)
        label = ROLE_REGISTRY.get(norm, {}).get("label", norm)
        esc = human_escalation_for(norm) or ""
        return False, (
            f"组织角色「{label}」未获授权执行 {tool_name} (需要能力: {cap}); "
            f"越权已拦截, 升级路径: {esc}"
        )
    amount = detect_amount(arguments)
    if amount is not None and amount > 0:
        verdict = fund_approval(norm, amount)
        if not verdict["allowed"]:
            label = ROLE_REGISTRY.get(norm, {}).get("label", norm)
            return False, (
                f"金额 ¥{amount:,.0f} 超出组织角色「{label}」的审批权限 "
                f"(需 {verdict['tier_label']} 级审批); 升级路径: {verdict.get('escalation', '')}"
            )
    return True, ""


def role_capability_brief(role: str) -> str:
    """组织角色能力包 (中文行为契约) — 注入 executor/会话 system 段。"""
    norm = normalize_role(str(role or ""))
    meta = ROLE_REGISTRY.get(norm)
    if not meta:
        return ""
    caps = sorted(MATRIX.get(norm, set()))
    return (
        f"【组织角色能力包】你在本组织中的角色: {meta['label']} (org_role={norm})"
        f" — {meta['description']}。\n"
        f"授权能力: {', '.join(caps) if caps else '(无)'}。\n"
        "未授权的操作 (越权写入/命令执行/超出角色额度的资金动作) 会被组织门禁直接拦截并转人工"
        " — 不要尝试绕过; 判断需要越权时, 停下并把决策交给人类审批。"
    )


def org_gate_approver(inner: Any, role: str) -> Any:
    """包装既有 approver: 组织门禁先行, 放行后进原审批链 (人兜底不变)。
    role 为空/未知 → 原样返回 inner (零行为变化)。"""
    norm = normalize_role(str(role or ""))
    if not norm or norm not in MATRIX:
        return inner

    async def gated(request: Any) -> Any:
        from .engine import ApprovalOutcome

        allowed, reason = org_gate(norm, request.tool_name, request.arguments)
        if not allowed:
            logger.info("org gate deny (%s): %s", norm, reason)
            request.reason = reason
            return ApprovalOutcome.DENY
        return await inner(request)

    return gated
