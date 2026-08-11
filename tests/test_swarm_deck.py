"""Tests for P0 建议3 — 蜂群指挥台: runtime DAG edits.

Covers: Task.agent default, the RunController structured queues, the
orchestrator draining an injected fork task mid-run, pending-task retargeting,
and the run store's parent_run_id (fork lineage). No network, no real LLM —
ScriptedProvider pops canned turns.
"""

from __future__ import annotations

import pytest

from coworker.orchestrator import Orchestrator, Plan, Task
from coworker.orchestrator.control import RunController
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient


class ScriptedProvider(ProviderClient):
    def __init__(self, turns):
        self._turns = list(turns)

    def complete(self, *, model, messages, tools=None, **settings):
        assert self._turns, "no scripted turn left"
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def _orch(tmp_path, provider, **kw):
    return Orchestrator(
        provider=provider,
        model="test-model",
        workspace=str(tmp_path / "ws"),
        **kw,
    )


def _accepted(text: str) -> AssistantTurn:
    return AssistantTurn(
        text=f'{{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}}',
        finish_reason="stop",
    )


def test_task_agent_defaults_empty():
    assert Task(id="t0", description="x").agent == ""
    assert Task(id="t0", description="x", agent="code").agent == "code"


def test_controller_queues_inject_and_retarget():
    ctrl = RunController()
    ctrl.inject_task(id="t9", description="extra", deps=["t0"], agent="code")
    ctrl.retarget_task("t1", "code")
    injections = ctrl.drain_task_injections()
    assert injections == [
        {"id": "t9", "description": "extra", "deps": ["t0"], "agent": "code"}
    ]
    assert ctrl.drain_task_injections() == []  # drained
    assert ctrl.drain_retargets() == [{"id": "t1", "agent": "code"}]


async def test_orchestrator_injects_fork_task_mid_run(tmp_path):
    """A task injected via the deck's queue joins the live plan and runs once
    its deps complete — the run finishes with all three tasks done."""
    provider = ScriptedProvider(
        [
            AssistantTurn(
                text='[{"id":"t0","description":"A","deps":[]},'
                     '{"id":"t1","description":"B","deps":["t0"]}]'
            ),
            AssistantTurn(text="A done. " + "x" * 100, finish_reason="stop"),
            _accepted("a"),
            AssistantTurn(text="B done. " + "x" * 100, finish_reason="stop"),
            _accepted("b"),
            AssistantTurn(text="C done. " + "x" * 100, finish_reason="stop"),
            _accepted("c"),
        ]
    )
    ctrl = RunController()
    orch = _orch(tmp_path, provider, controller=ctrl)
    ctrl.inject_task(id="t2", description="C", deps=["t0"], agent="code")
    result = await orch.run("goal")
    assert result.status == "completed"
    by_id = result.plan.by_id()
    assert set(by_id) == {"t0", "t1", "t2"}
    assert all(t.done for t in result.plan.tasks)
    assert by_id["t2"].agent == "code"


async def test_orchestrator_retargets_pending_task(tmp_path):
    provider = ScriptedProvider(
        [
            AssistantTurn(
                text='[{"id":"t0","description":"A","deps":[]},'
                     '{"id":"t1","description":"B","deps":[]}]'
            ),
            AssistantTurn(text="A done. " + "x" * 100, finish_reason="stop"),
            _accepted("a"),
            AssistantTurn(text="B done. " + "x" * 100, finish_reason="stop"),
            _accepted("b"),
        ]
    )
    ctrl = RunController()
    orch = _orch(tmp_path, provider, controller=ctrl)
    # retarget BEFORE the run: the first scheduling round applies it.
    ctrl.retarget_task("t1", "code")
    result = await orch.run("goal")
    assert result.plan.by_id()["t1"].agent == "code"
    assert result.plan.by_id()["t0"].agent == ""


async def test_initial_plan_skips_planner(tmp_path):
    """A fork run (initial_plan) executes the given graph without the planner."""
    provider = ScriptedProvider(
        [
            AssistantTurn(text="direct result. " + "x" * 100, finish_reason="stop"),
            _accepted("ok"),
        ]
    )
    plan = Plan(goal="g", tasks=[Task(id="t0", description="direct", deps=[])])
    orch = _orch(tmp_path, provider, initial_plan=plan)
    result = await orch.run("g")
    assert result.status == "completed"
    assert result.runs == 1  # no planner turn consumed — plan was pre-built


def test_run_store_parent_run_id(tmp_path):
    """Fork lineage: create_run(parent_run_id=…) persists and surfaces the link."""
    from coworker.orchestrator.run_store import OrchestrationRunStore

    store = OrchestrationRunStore(tmp_path / "runs.db")
    parent = store.create_run("original")
    fork = store.create_run("branch A", parent_run_id=parent)
    assert store.get_run(fork)["parent_run_id"] == parent
    assert store.get_run(parent)["parent_run_id"] is None
    listed = {r["run_id"]: r for r in store.list_runs()}
    assert listed[fork]["parent_run_id"] == parent
    # legacy schema (no parent column) still works — covered by fresh-create path


def test_control_api_task_inject_and_retarget(tmp_path, monkeypatch):
    """POST /v1/orchestrate/{id}/control with the deck's new DAG-edit actions."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    manager = SessionManager(data_dir=tmp_path / "data")
    client = TestClient(create_app(manager))

    run_id = manager.orchestration_store.create_run("deck test")
    ctrl = RunController()
    manager.active_orchestration_controls[run_id] = ctrl

    r = client.post(
        f"/v1/orchestrate/{run_id}/control",
        json={"action": "task_inject", "task_id": "t9", "description": "extra", "deps": ["t0"]},
    )
    assert r.json()["ok"] is True
    assert ctrl.drain_task_injections()[0]["id"] == "t9"

    r = client.post(
        f"/v1/orchestrate/{run_id}/control",
        json={"action": "retarget", "task_id": "t1", "agent": "code"},
    )
    assert r.json()["ok"] is True
    assert ctrl.drain_retargets() == [{"id": "t1", "agent": "code"}]

    # invalid retarget agent rejected
    r = client.post(
        f"/v1/orchestrate/{run_id}/control",
        json={"action": "retarget", "task_id": "t1", "agent": "hacker"},
    )
    assert r.json()["ok"] is False


def test_dissolve_run_lifecycle(tmp_path, monkeypatch):
    """P0 增量2: a finished run can be dissolved (terminal status + event);
    an active run is refused; dissolving twice is idempotent."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    manager = SessionManager(data_dir=tmp_path / "data")
    client = TestClient(create_app(manager))
    store = manager.orchestration_store

    rid = store.create_run("finished goal")
    store.update_status(rid, "completed")

    # dissolve a finished run
    r = client.post(f"/v1/orchestrate/{rid}/dissolve", json={})
    assert r.json()["ok"] is True
    run = store.get_run(rid)
    assert run["status"] == "dissolved"
    assert any(ev["kind"] == "run_dissolved" for ev in run["events"])
    # list_runs surfaces the terminal status
    assert store.list_runs()[0]["status"] == "dissolved"

    # idempotent
    r2 = client.post(f"/v1/orchestrate/{rid}/dissolve", json={})
    assert r2.json()["ok"] is True and r2.json()["already"] is True

    # active run refused
    active = store.create_run("live goal")
    from coworker.orchestrator.control import RunController

    manager.active_orchestration_controls[active] = RunController()
    r3 = client.post(f"/v1/orchestrate/{active}/dissolve", json={})
    assert r3.json()["ok"] is False
    assert "still active" in r3.json()["error"]

    # missing run
    r4 = client.post("/v1/orchestrate/nope/dissolve", json={})
    assert r4.json()["ok"] is False
