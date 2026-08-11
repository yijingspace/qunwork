"""Tests for P0 增量3 — 组织级权限矩阵 (角色×能力 + 资金分级审批)."""

from __future__ import annotations

import pytest

from coworker.permission_matrix import (
    can,
    detect_amount,
    fund_approval,
    fund_tier,
)


# -- fund tiers --------------------------------------------------------------
def test_fund_tier_three_levels():
    small = fund_tier(1_000)
    assert small["role"] == "general_manager"
    assert small["label"] == "总经理级"

    mid = fund_tier(50_000)
    assert mid["role"] == "chairman"
    assert mid["label"] == "董事长级"

    large = fund_tier(5_000_000)
    assert large["role"] == "board_human"
    assert large["label"] == "董事会合议 + 人类终审"

    # boundaries: exactly 5k and 500k
    assert fund_tier(5_000)["role"] == "general_manager"
    assert fund_tier(500_000)["role"] == "chairman"


# -- role × capability -------------------------------------------------------
def test_can_fail_closed():
    assert can("chairman", "project_group") is True
    assert can("general_manager", "issue_commands") is True
    # worker cannot issue commands or approve funds
    assert can("worker", "issue_commands") is False
    assert can("worker", "fund:general_manager") is False
    # unknown role → everything denied
    assert can("ghost", "read_memory") is False
    # scope-sensitive: worker may write business data, not risk logs
    assert can("worker", "write_memory:business") is True
    assert can("worker", "write_memory:risk") is False
    assert can("compliance", "write_memory:risk") is True


# -- fund approval -----------------------------------------------------------
def test_fund_approval_allow_and_escalate():
    # chairman approves up to 500k
    ok = fund_approval("chairman", 100_000)
    assert ok["allowed"] is True
    # worker cannot approve anything monetary → escalate to required tier
    denied = fund_approval("worker", 30_000)
    assert denied["allowed"] is False
    assert denied["required_tier"] == "chairman"
    assert "escalation" in denied and denied["escalation"]
    # a large payment escalates to board + human, with the board's escalation note
    big = fund_approval("general_manager", 5_000_000)
    assert big["allowed"] is False
    assert big["required_tier"] == "board_human"


def test_human_escalation_for():
    from coworker.permission_matrix import human_escalation_for

    assert "红线" in human_escalation_for("chairman")
    assert human_escalation_for("unknown") is None


# -- amount detection --------------------------------------------------------
def test_detect_amount_various_keys():
    assert detect_amount({"amount": 1200}) == 1200
    assert detect_amount({"money": "50000"}) == 50000
    assert detect_amount({"price": 99.5}) == 99.5
    assert detect_amount({"金额": 300}) == 300
    assert detect_amount({"transfer_amount": 8800}) == 8800
    # no amount → None; non-numeric → None; non-dict → None
    assert detect_amount({"path": "/tmp/x"}) is None
    assert detect_amount({"amount": "abc"}) is None
    assert detect_amount(None) is None


# -- approval card annotation ------------------------------------------------
def test_approval_body_annotates_fund_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from coworker.server.manager import _approval_body

    class _Req:
        def __init__(self, args, reason=""):
            self.arguments = args
            self.reason = reason

    body = _approval_body(_Req({"amount": 12_000}, reason="pay vendor"))
    assert "pay vendor" in body
    assert "董事长级" in body  # 12k → chairman tier
    # no amount → no annotation
    plain = _approval_body(_Req({"path": "/tmp/x"}))
    assert "金额" not in plain


def test_permission_matrix_api(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    manager = SessionManager(data_dir=tmp_path / "data")
    client = TestClient(create_app(manager))
    r = client.get("/v1/permission-matrix")
    d = r.json()
    assert "chairman" in d["matrix"]
    assert d["matrix"]["worker"]  # non-empty capability set
    tiers = {t["role"] for t in d["fund_tiers"]}
    assert tiers == {"general_manager", "chairman", "board_human"}
    assert "human_escalation" in d
