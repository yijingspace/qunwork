"""Tests for 13 影子模式 — 决策回放离线回退 (LOW-4 修复).

覆盖: audit_events 持久化 decision_trace entry(payload 列) + query 读回 +
session_decision_trace 在 engine 销毁后从 audit 回退返回完整 trace。
"""

from __future__ import annotations

import json

from coworker.audit import AuditStore


def test_audit_persists_decision_entry_payload(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    store.append(
        {
            "session_id": "sess-1",
            "agent": "cowork",
            "stage": "decision_trace",
            "decision_kind": "tool_selection",
            "entry": {
                "ts": 123.0,
                "iteration": 1,
                "kind": "tool_selection",
                "agent": "cowork",
                "session_id": "sess-1",
                "tool": "write_file",
                "reason": "save the report",
            },
        }
    )
    rows = store.query(limit=100)
    assert len(rows) == 1
    assert rows[0]["stage"] == "decision_trace"
    # entry 原样持久化并可读回 (修复前 payload 列不存在, 恒空)
    entry = rows[0]["payload"]
    assert isinstance(entry, dict)
    assert entry["kind"] == "tool_selection"
    assert entry["reason"] == "save the report"
    assert entry["session_id"] == "sess-1"


def test_session_decision_trace_falls_back_to_audit(tmp_path, monkeypatch):
    """engine 销毁后(未注册 live engine) → session_decision_trace 从 audit
    回退返回完整 trace (修复前 query 方法不存在 + entry 未持久化 → 恒空)。"""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from coworker.server.manager import SessionManager

    mgr = SessionManager(data_dir=tmp_path / "data")
    # 直接向 audit 写入一条 decision_trace (模拟已销毁 engine 的离线记录)
    mgr.audit_store.append(
        {
            "session_id": "sess-offline",
            "agent": "cowork",
            "stage": "decision_trace",
            "decision_kind": "permission",
            "entry": {
                "ts": 456.0,
                "iteration": 2,
                "kind": "permission",
                "agent": "cowork",
                "session_id": "sess-offline",
                "tool": "github__create_pr",
                "allowed": False,
                "reason": "scope escalation",
            },
        }
    )
    # 不注册 live engine → 走 audit 回退
    out = mgr.session_decision_trace("sess-offline")
    assert out["source"] == "audit_store"
    assert len(out["trace"]) == 1
    assert out["trace"][0]["kind"] == "permission"
    assert out["trace"][0]["reason"] == "scope escalation"

    # 其他 session 不串扰
    other = mgr.session_decision_trace("sess-other")
    assert other["trace"] == []
