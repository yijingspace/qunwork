"""Tests for P1 — 团队 / Agent 状态池 / 任务组生命周期.

Unit tests that do NOT need a real Manager or LLM:
  - AgentPool lifecycle (register / acquire / release / heartbeat / fault)
  - TeamStore CRUD (members / agents snapshot / task groups)
  - TaskLifecycle transitions + dissolve auto-cleanup
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from coworker.team import AgentPool, AgentState, TaskLifecycle, TeamStore


# ======================================================================
# AgentPool
# ======================================================================
def test_pool_register_list_and_unregister():
    pool = AgentPool()
    a = pool.register("chairman", "chairman_persona")
    assert a.role == "chairman"
    assert a.persona_id == "chairman_persona"
    assert a.state == AgentState.IDLE
    # Pool.get() returns a (thread-safe) copy — compare by identity fields.
    got = pool.get(a.id)
    assert got is not None
    assert got.id == a.id
    assert got.role == a.role
    assert [inst.id for inst in pool.list()] == [a.id]

    assert pool.unregister(a.id) is True
    assert pool.list() == []


def test_acquire_picks_lowest_load_and_marks_working():
    pool = AgentPool()
    a1 = pool.register("worker", "w1")
    a2 = pool.register("worker", "w2")
    # raise a2's load so a1 should be preferred
    a2.load = 0.8
    got = pool.acquire("worker", task_group_id="g1", task_id="t1")
    assert got is a1
    assert a1.state is AgentState.WORKING
    assert a1.current_task_group == "g1"
    assert a1.current_task_id == "t1"
    assert a1.load > 0.0  # load was bumped by acquire


def test_acquire_respects_role_and_availability():
    pool = AgentPool()
    w = pool.register("worker", "w")
    r = pool.register("reviewer", "r")
    # busy out the only worker
    busy = pool.acquire("worker", task_id="t1")
    assert busy is w
    # No available worker anymore → None returned
    assert pool.acquire("worker", task_id="t2") is None
    # Reviewer pool has capacity though
    assert pool.acquire("reviewer", task_id="t3") is r


def test_release_restores_idle_state():
    pool = AgentPool()
    a = pool.register("worker", "w")
    got = pool.acquire("worker", task_group_id="g", task_id="t")
    assert got.state is AgentState.WORKING
    pool.release(got.id)
    assert got.state is AgentState.IDLE
    assert got.current_task_group is None
    assert got.current_task_id is None


def test_fault_cleanup_marks_idle_agents_working_as_fault():
    pool = AgentPool()
    a = pool.register("worker", "w")
    # make the agent look busy without heartbeating
    a.state = AgentState.WORKING
    a.last_heartbeat = a.last_heartbeat - 100.0  # far in the past
    # reap_stale / cleanup_faulty marks working-without-heartbeat agents as FAULT.
    pool.cleanup_faulty(timeout=1.0)
    assert a.state is AgentState.FAULT


# ======================================================================
# TeamStore (persistence)
# ======================================================================
def _tmp_store() -> tuple[TeamStore, Path]:
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "t.db"
    return TeamStore(db), tmp


def test_team_ensure_and_summary():
    store, _ = _tmp_store()
    s1 = store.ensure_team(name="Bee Inc")
    s2 = store.ensure_team(name="Bee Too")
    assert s1["name"] == "Bee Inc"
    # Ensure is idempotent — the name does not mutate once seeded.
    assert s2["name"] == "Bee Inc"


def test_member_crud():
    store, _ = _tmp_store()
    store.ensure_team()
    m = store.add_member("Anna", role="gm")
    assert m["name"] == "Anna"
    assert m["role"] == "gm"

    members = store.list_members()
    assert any(x["id"] == m["id"] for x in members)

    ok = store.update_member(m["id"], role="scheduler")
    assert ok is True
    got = next(x for x in store.list_members() if x["id"] == m["id"])
    assert got["role"] == "scheduler"

    assert store.remove_member(m["id"]) is True
    assert not any(x["id"] == m["id"] for x in store.list_members())


def test_agent_snapshot_round_trip():
    store, _ = _tmp_store()
    store.ensure_team()
    pool = AgentPool()
    pool.register("worker", "w1")
    pool.register("reviewer", "r1")
    store.save_agent_snapshot(pool.list())
    recovered = store.load_agent_snapshot()
    assert sorted(a["role"] for a in recovered) == ["reviewer", "worker"]


def test_task_group_lifecycle_in_store():
    store, _ = _tmp_store()
    store.ensure_team()
    g = store.create_task_group("Ship v1.2", member_ids=["m1", "m2"], agent_ids=["a1"])
    assert g["state"] == "forming"
    assert g["member_ids"] == ["m1", "m2"]

    # update_task_group_state returns bool; we re-read via get_task_group.
    assert store.update_task_group_state(g["id"], "active") is True
    after = store.get_task_group(g["id"])
    assert after is not None
    assert after["state"] == "active"

    # list active only
    active_only = store.list_task_groups(include_dissolved=False)
    assert any(x["id"] == g["id"] for x in active_only)

    # dissolve — sets dissolved_at (we verify it's not null after).
    assert store.update_task_group_state(g["id"], "dissolved") is True
    not_there = store.list_task_groups(include_dissolved=False)
    assert not any(x["id"] == g["id"] for x in not_there)
    full = store.list_task_groups(include_dissolved=True)
    dissolved = next(x for x in full if x["id"] == g["id"])
    assert dissolved["dissolved_at"] is not None


# ======================================================================
# TaskLifecycle — state machine + dissolve cleanup
# ======================================================================
def test_lifecycle_valid_transitions():
    store, _ = _tmp_store()
    pool = AgentPool()
    lc = TaskLifecycle(store, pool)
    g = lc.create("Goal")

    g = lc.transition(g["id"], "active")
    assert g["state"] == "active"
    g = lc.transition(g["id"], "reviewing")
    assert g["state"] == "reviewing"
    g = lc.transition(g["id"], "dissolved")
    assert g["state"] == "dissolved"


def test_lifecycle_invalid_transition_errors():
    store, _ = _tmp_store()
    pool = AgentPool()
    lc = TaskLifecycle(store, pool)
    g = lc.create("Goal")
    with pytest.raises(ValueError):
        # forming → reviewing is not allowed
        lc.transition(g["id"], "reviewing")


def test_dissolve_releases_agents_and_runs_ingest_hook():
    store, _ = _tmp_store()
    pool = AgentPool()
    a = pool.register("worker", "w")
    pool.register("reviewer", "r")
    # acquire them so they look busy + tagged with our group
    g = store.create_task_group("Dissolve test", agent_ids=[a.id])
    # "work" them
    pool.acquire("worker", task_group_id=g["id"], task_id="t")
    assert a.state is AgentState.WORKING

    calls: list[str] = []

    def _ingest(group_id: str):
        calls.append(group_id)

    lc = TaskLifecycle(store, pool, ingest_swarm_assets=_ingest)
    result = lc.dissolve(g["id"])
    assert result["dissolved"] is True
    assert a.state is AgentState.IDLE
    assert calls == [g["id"]]
    # idempotent — dissolving twice does not blow up.
    twice = lc.dissolve(g["id"])
    assert twice["already_dissolved"] is True


# -- API 层回归（本次修复: HTTPException import / /v1/team/permissions / dissolve 归档）--

def _team_client(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from fastapi.testclient import TestClient
    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    mgr = SessionManager(data_dir=tmp_path / "data")
    return TestClient(create_app(mgr))


def test_api_illegal_transition_returns_400(tmp_path, monkeypatch):
    """非法状态转移必须返回 400（修复前 HTTPException 未 import → NameError 500）。"""
    client = _team_client(tmp_path, monkeypatch)
    gm = client.post("/v1/team/members", json={"name": "gm", "role": "gm"}).json()["id"]
    gid = client.post("/v1/team/task-groups", json={"goal": "g", "owner_member": gm}).json()["id"]
    client.post(f"/v1/team/task-groups/{gid}/transition", json={"state": "active"})
    r = client.post(f"/v1/team/task-groups/{gid}/transition", json={"state": "forming"})
    assert r.status_code == 400
    # 未知组 → 404
    assert client.post("/v1/team/task-groups/nope/transition", json={"state": "active"}).status_code == 404


def test_api_team_permissions_shape(tmp_path, monkeypatch):
    """GET /v1/team/permissions 返回前端 PermissionMatrix 格式(roles + thresholds)。"""
    client = _team_client(tmp_path, monkeypatch)
    d = client.get("/v1/team/permissions").json()
    assert "chairman" in d["roles"] and "worker" in d["roles"]
    assert d["roles"]["chairman"]["project_group"]["allowed"] is True
    assert d["roles"]["worker"].get("issue_commands") is None  # fail-closed
    t = d["thresholds"]
    assert [x["approver_role"] for x in t] == ["general_manager", "chairman", "board_human"]
    assert t[0]["max_amount"] == 5000 and t[2]["require_human"] is True


def test_api_dissolve_archives_to_knowledge(tmp_path, monkeypatch):
    """dissolve 后蜂群归档必须写入 knowledge（修复前 knowledge.add 不存在 → 归档静默失败）。"""
    client = _team_client(tmp_path, monkeypatch)
    gid = client.post("/v1/team/task-groups", json={"goal": "归档测试"}).json()["id"]
    assert client.post(f"/v1/team/task-groups/{gid}/dissolve", json={}).json()["dissolved"] is True
    items = client.get("/v1/knowledge").json().get("items", [])
    assert any("[蜂群归档]" in k.get("title", "") for k in items)
