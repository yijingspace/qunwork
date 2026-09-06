"""方案B 角色即能力包: org gate + persona org_role + approver 包装 (2026-09-06)."""

from __future__ import annotations

import asyncio

import pytest

from coworker.permission_matrix import (
    org_gate,
    org_gate_approver,
    role_can_tool,
    role_capability_brief,
)


class _Req:
    """PermissionRequest 形状的最小替身。"""

    def __init__(self, tool_name, arguments=None):
        self.tool_name = tool_name
        self.arguments = arguments or {}
        self.reason = ""


# -- 决策表 ----------------------------------------------------------------------


def test_unmapped_tools_are_not_gated():
    """未映射工具 (读/搜索) 不受组织门禁 — 约束是叠加不是替代。"""
    assert role_can_tool("worker", "read_file") is True
    allowed, _ = org_gate("worker", "read_file", {})
    assert allowed is True


def test_worker_gated_on_shell_allowed_on_write():
    """工蜂: write_memory:business → 可写文件; 无 issue_commands → shell 拦。"""
    ok, why = org_gate("worker", "write_file", {"path": "a.md", "content": "x"})
    assert ok is True
    ok, why = org_gate("worker", "run_shell", {"command": "ls"})
    assert ok is False and "run_shell" in why and "升级" in why


def test_chairman_full_commands_allowed():
    ok, _ = org_gate("chairman", "run_shell", {"command": "ls"})
    assert ok is True


def test_fund_tier_gate():
    """两道关: 能力关先行 (worker 无 issue_commands → send_message 能力拦),
    有能力的角色再走金额分级关 (gm 可发小额, 80w 需董事会)。"""
    ok, why = org_gate("worker", "send_message", {"amount": 100})
    assert ok is False and "send_message" in why  # 能力关拦下
    # 总经理: ≤5k 自批; >50w 必须董事会+人类终审 (连董事长也不能自批 board 级)
    ok, _ = org_gate("general_manager", "send_message", {"amount": 3_000})
    assert ok is True
    ok, _ = org_gate("chairman", "send_message", {"amount": 300_000})
    assert ok is True  # chairman 级 ≤50w
    ok, why = org_gate("chairman", "send_message", {"amount": 800_000})
    assert ok is False and "¥800,000" in why and "董事会" in why
    # board 角色无 send 能力 (纯投票/终审位) — 能力关拦, 到不了金额关
    ok, _ = org_gate("board", "send_message", {"amount": 100})
    assert ok is False


def test_org_role_alias_and_unknown_role():
    """历史别名 gm 规范化后受约束; 未知角色不 gate (现状)。"""
    ok, _ = org_gate("gm", "run_shell", {"command": "ls"})
    assert ok is True  # general_manager 有 issue_commands
    ok, _ = org_gate("supreme_leader", "run_shell", {"command": "rm -rf /"})
    assert ok is True  # 未注册 → 不 gate (审批兜底仍生效)
    ok, _ = org_gate("", "run_shell", {})
    assert ok is True


def test_capability_brief_text():
    brief = role_capability_brief("reviewer")
    assert "审校" in brief and "reviewer" in brief
    assert "组织门禁" in brief
    assert role_capability_brief("unknown") == ""


# -- approver 包装 ---------------------------------------------------------------


def test_org_gate_approver_deny_short_circuits_inner():
    calls = []

    async def inner(req):
        calls.append(req.tool_name)
        return "once"

    gated = org_gate_approver(inner, "worker")
    # 未映射工具 → 进原审批链
    assert asyncio.run(gated(_Req("read_file"))) == "once"
    # 越权 shell → DENY, 不打扰人类审批
    from coworker.engine import ApprovalOutcome

    req = _Req("run_shell", {"command": "ls"})
    assert asyncio.run(gated(req)) is ApprovalOutcome.DENY
    assert "run_shell" in req.reason  # 拒绝原因回填给模型/审计
    assert calls == ["read_file"]


def test_org_gate_approver_noop_without_role():
    async def inner(req):
        return "once"

    assert org_gate_approver(inner, None) is inner
    assert org_gate_approver(inner, "ghost_role") is inner


# -- persona manifest 声明 --------------------------------------------------------


from coworker.personas.manifest import ManifestError, parse_manifest

_BASE = """---
id: p-auditor
name: 审计官
family: knowledge
{extra}
---
你是审计官, 只出报告。
"""


def test_manifest_org_role_parsed_and_normalized():
    m = parse_manifest(_BASE.format(extra="org_role: auditor"))
    assert m.org_role == "auditor"
    m2 = parse_manifest(_BASE.format(extra="org_role: gm"))  # 别名规范化
    assert m2.org_role == "general_manager"
    assert m2.to_agent().org_role == "general_manager"
    # 未声明 → None (现状)
    assert parse_manifest(_BASE.format(extra="")).org_role is None


def test_manifest_org_role_rejects_unknown():
    with pytest.raises(ManifestError):
        parse_manifest(_BASE.format(extra="org_role: supreme_leader"))
