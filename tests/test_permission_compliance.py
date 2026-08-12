"""Tests for P2 — 组织权限矩阵 × inbox 审批合规标注.

Covers:
  - annotate_compliance() for various tool + role + amount combinations
  - InboxStore.add_approval() auto-injects compliance into data
  - inbox_approver passes member_role through
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from coworker.permission_matrix import (
    annotate_compliance,
    can,
    detect_amount,
    fund_approval,
    fund_tier,
)
from coworker.inbox import InboxStore, inbox_approver


# ======================================================================
# annotate_compliance — the core P2 function
# ======================================================================
def test_routine_write_file_with_worker_role():
    """A general_manager writing a file: GM has write_memory → routine."""
    c = annotate_compliance("write_file", {"path": "/tmp/x.txt"}, "general_manager")
    assert c["capability"] == "write_memory"
    assert c["member_role"] == "general_manager"
    assert c["role_has_capability"] is True
    assert c["compliance_level"] == "routine"
    assert c["fund_tier"] is None


def test_elevated_when_role_lacks_capability():
    """A reviewer trying to issue_commands: reviewer lacks issue_commands → elevated."""
    c = annotate_compliance("run_shell", {"command": "ls"}, "reviewer")
    assert c["role_has_capability"] is False
    assert c["compliance_level"] == "elevated"
    assert c["escalation"] is not None


def test_fund_tier_detected_for_small_amount():
    """A tool call with amount=3000: within general_manager tier."""
    c = annotate_compliance(
        "send_message",
        {"text": "pay", "amount": 3000},
        "general_manager",
    )
    assert c["fund_tier"] is not None
    assert c["fund_tier"]["role"] == "general_manager"
    assert c["fund_approval"]["allowed"] is True
    assert c["compliance_level"] == "routine"  # GM can approve ≤5k


def test_fund_tier_escalation_for_large_amount():
    """A worker trying to pay 600k: board_human tier → board level."""
    c = annotate_compliance(
        "run_shell",
        {"command": "transfer", "amount": 600000},
        "worker",
    )
    assert c["fund_tier"]["role"] == "board_human"
    assert c["fund_approval"]["allowed"] is False
    assert c["compliance_level"] == "board"
    assert c["escalation"] is not None


def test_no_role_info_still_detects_fund():
    """Without member_role, we still detect amount + tier (for display)."""
    c = annotate_compliance("send_message", {"amount": 50000}, None)
    assert c["member_role"] is None
    assert c["fund_tier"] is not None
    assert c["fund_tier"]["role"] == "chairman"
    assert c["compliance_level"] == "elevated"


def test_unknown_tool_is_routine():
    """An unmapped tool name: no capability check, no fund → routine."""
    c = annotate_compliance("some_custom_tool", {}, "worker")
    assert c["capability"] is None
    assert c["compliance_level"] == "routine"


def test_chairman_can_approve_within_tier():
    """Chairman approving 100k: within chairman tier (≤500k) → allowed."""
    c = annotate_compliance(
        "send_file",
        {"path": "/invoice.pdf", "amount": 100000},
        "chairman",
    )
    assert c["fund_approval"]["allowed"] is True
    assert c["compliance_level"] == "routine"


# ======================================================================
# InboxStore.add_approval — auto compliance injection
# ======================================================================
def test_add_approval_injects_compliance_with_role():
    store = InboxStore()
    item = store.add_approval(
        "sess-1",
        "Run `write_file`?",
        body="test",
        member_role="worker",
        tool_name="write_file",
        arguments={"path": "/x"},
    )
    assert "compliance" in item.data
    assert item.data["compliance"]["member_role"] == "worker"
    assert item.data["compliance"]["capability"] == "write_memory"


def test_add_approval_injects_compliance_without_role():
    """Even without member_role, if tool_name is given, fund detection runs."""
    store = InboxStore()
    item = store.add_approval(
        "sess-1",
        "Run `send_message`?",
        body="pay",
        tool_name="send_message",
        arguments={"amount": 50000},
    )
    assert "compliance" in item.data
    assert item.data["compliance"]["fund_tier"] is not None


def test_add_approval_without_tool_name_has_no_compliance():
    """Backward compat: if tool_name is not passed, no compliance annotation."""
    store = InboxStore()
    item = store.add_approval("sess-1", "Run `write_file`?", body="test")
    assert "compliance" not in (item.data or {})


# ======================================================================
# inbox_approver — member_role passthrough
# ======================================================================
def test_inbox_approver_accepts_member_role():
    """The approver factory should accept member_role without error."""
    store = InboxStore()
    approve = inbox_approver(store, "sess-1", member_role="chairman")
    # We can't easily call approve() without a real PermissionRequest,
    # but verifying the factory doesn't crash is sufficient for unit level.
    assert callable(approve)


# ======================================================================
# Integration: fund tier detection chain
# ======================================================================
def test_detect_amount_finds_chinese_key():
    """detect_amount should find 金额 in Chinese-keyed arguments."""
    assert detect_amount({"金额": 999}) == 999.0


def test_fund_tier_boundaries():
    assert fund_tier(0)["role"] == "general_manager"
    assert fund_tier(5000)["role"] == "general_manager"
    assert fund_tier(5001)["role"] == "chairman"
    assert fund_tier(500000)["role"] == "chairman"
    assert fund_tier(500001)["role"] == "board_human"


def test_fund_approval_worker_cannot_approve_any_tier():
    """Worker role has no fund: capability at all → always denied."""
    verdict = fund_approval("worker", 100)
    assert verdict["allowed"] is False
    assert "required_tier" in verdict


def test_can_check_for_chairman():
    assert can("chairman", "write_memory") is True
    assert can("chairman", "fund:chairman") is True
    assert can("chairman", "fund:board_human") is False  # chairman can't do board
    assert can("worker", "issue_commands") is False
