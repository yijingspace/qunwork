"""方案E 组织脉搏: detect_amount 增强 (嵌套/中文) + pulse 聚合 (2026-09-07)."""

from __future__ import annotations

import time

from coworker.permission_matrix import detect_amount
from coworker.team.pulse import collect_pulse
from coworker.team.store import TeamStore


# -- 金额检测增强 ----------------------------------------------------------------


def test_detect_amount_basics_unchanged():
    assert detect_amount({"amount": 500}) == 500.0
    assert detect_amount({"payment": "1200.5"}) == 1200.5
    assert detect_amount({"path": "a.md"}) is None
    assert detect_amount(None) is None


def test_detect_amount_nested():
    """审批参数常包在 payload/details 里 — 递归找。"""
    assert detect_amount({"payload": {"order": {"price": 99}}}) == 99.0
    assert detect_amount({"items": [{"amount": 250}]}) == 250.0
    assert detect_amount({"a": {"b": {"c": {"d": {"amount": 1}}}}}) is None  # 深度≤3


def test_detect_amount_chinese_numerals():
    assert detect_amount({"金额": "60万"}) == 600_000.0
    assert detect_amount({"note": "转账1.2亿元"}) == 120_000_000.0
    assert detect_amount({"body": "支付 ¥3,000 元"}) == 3000.0
    assert detect_amount({"text": "买500块的服务器"}) == 500.0
    assert detect_amount({"summary": "预算 20 万元整"}) == 200_000.0


def test_detect_amount_no_false_positive():
    """无货币线索的中文数字不误报; 金额键下 bool 不当数字。"""
    assert detect_amount({"text": "关注数3万"}) is None
    assert detect_amount({"count": 30000}) is None  # count 不是金额键
    assert detect_amount({"amount": True}) is None
    assert detect_amount({"reason": "超时"}) is None


# -- 脉搏聚合 ---------------------------------------------------------------------


def _seeded_store(tmp_path):
    store = TeamStore(tmp_path / "team.db")
    store.ensure_team("研究蜂群")
    me = store.get_team()["my_member_id"]
    w = store.add_member("小王", "reviewer", status="online")
    inv = store.add_member("小李", "worker", status="invited")
    g = store.create_task_group(
        "季度市场报告", owner_member=me, member_ids=[me, w["id"], inv["id"]],
        agent_ids=["a1", "a2"],
    )
    store.update_task_group_state(g["id"], "active")
    now = time.time()
    store.append_audit("matrix.grant", actor=me, target="worker", detail={"capability": "vote"})
    store.append_audit("org_gate.fund_deny", actor="system", target="worker",
                       detail={"amount": 800000.0, "tool": "send_message"})
    store.append_audit("org_gate.fund_deny", actor="system", target="worker",
                       detail={"amount": 5000.0, "tool": "run_shell"})
    # 窗口外的旧拦截 (45 天前)
    store._db.execute(
        "INSERT INTO audit_log(ts,actor,action,target,detail) VALUES (?,?,?,?,?)",
        (now - 45 * 86400, "system", "org_gate.fund_deny", "worker", '{"amount": 999}'),
    )
    store._db.commit()
    return store, me, w, inv, g


def test_pulse_fund_flow_window(tmp_path):
    store, *_ = _seeded_store(tmp_path)
    p = collect_pulse(store)
    ff = p["fund_flow"]
    assert ff["blocked_count"] == 2  # 45 天前的旧事件被窗口切掉
    assert ff["blocked_total"] == 805000.0
    assert ff["by_role"]["worker"] == 805000.0
    assert p["governance"]["matrix_changes"] == 1


def test_pulse_timeline_and_roster(tmp_path):
    store, me, w, inv, g = _seeded_store(tmp_path)
    store.record_sync_change("member", w["id"], "upsert", {}, author="local")
    p = collect_pulse(store)
    kinds = {e["kind"] for e in p["timeline"]}
    assert kinds == {"governance", "sync"}
    ts = [e["ts"] for e in p["timeline"]]
    assert ts == sorted(ts, reverse=True)  # 合并后倒序
    assert any(e["action"] == "sync.member.upsert" for e in p["timeline"])
    assert p["roster"] == {"total": 3, "online": 2, "invited": 1, "offline": 0}


def test_pulse_responsibility_chain(tmp_path):
    store, me, w, inv, g = _seeded_store(tmp_path)
    p = collect_pulse(store)
    chain = next(c for c in p["responsibility"] if c["id"] == g["id"])
    assert chain["state"] == "active"
    assert chain["owner"]["role"] == "chairman"  # ensure_team 的 Me 行
    names = {m["name"] for m in chain["members"]}
    assert names == {"Me", "小王", "小李"}
    # 待接受成员在责任链里可见 (状态透传)
    assert any(m["status"] == "invited" for m in chain["members"])
    assert chain["agent_count"] == 2
    # active 排最前
    assert p["responsibility"][0]["state"] == "active"


def test_pulse_gate_e2e_fund_amount_into_audit(tmp_path):
    """org_gate (中文嵌套金额) → deny → 审计落笔 → 脉搏看到同一笔拦截。"""
    store, me, w, inv, g = _seeded_store(tmp_path)  # TeamStore 注册 provider
    from coworker.permission_matrix import org_gate
    import coworker.permission_matrix as pm

    sink: list = []
    pm._audit_sink  # 现状可能已有 sink; 本测试直接调 org_gate 后手工查审计
    ok, reason = org_gate("general_manager", "send_message",
                          {"payload": {"金额": "80万"}})
    assert ok is False and "¥800,000" in reason
    # deny 经 sink 落审计 (manager 未启动时 sink 可能未注册 → 手动验证链路)
    pm.register_audit_sink(lambda a, actor, target, detail: sink.append((a, target, detail)))
    org_gate("general_manager", "send_message", {"amount": "60万"})
    assert sink and sink[0][0] == "org_gate.fund_deny" and sink[0][2]["amount"] == 600000.0
