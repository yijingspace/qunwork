"""Tests for P0 增量1 — 信息素负载均衡 (stigmergic load field).

The PheromoneField is the coordination-plane load signal: tasks deposit a busy
mark on start and withdraw on completion; it evaporates over time; the
orchestrator reads it to shrink the parallel batch when the colony is loaded.
No network, no real LLM — ScriptedProvider drives the orchestrator.
"""

from __future__ import annotations

import asyncio

import pytest

from coworker.pheromone import PheromoneField


# -- the field itself ---------------------------------------------------------
def test_deposit_level_and_withdraw():
    f = PheromoneField(half_life=10_000)  # effectively no evaporation in-test
    assert f.level("cowork") == 0.0
    f.deposit("cowork", 1.0)
    assert f.level("cowork") == pytest.approx(1.0)
    f.deposit("cowork", -1.0)  # withdraw on completion
    assert f.level("cowork") == 0.0


def test_evaporation_over_time():
    # Deterministic clock — a real time.sleep on a 200ms half-life was flaky
    # under load (a preempted process evaporates past the >0.0 assertion).
    clock = {"t": 0.0}

    def _now():
        return clock["t"]

    f = PheromoneField(half_life=0.2, now_fn=_now)
    f.deposit("code", 1.0)
    assert f.level("code") > 0.9
    clock["t"] += 0.45  # > 2 half-lives → < 25% of original
    assert f.level("code") < 0.3
    assert f.level("code") > 0.0  # still faintly present before full fade


def test_busy_keys_and_total_load():
    f = PheromoneField(half_life=10_000)
    f.deposit("cowork", 1.0)
    f.deposit("code", 2.0)
    f.deposit("tiny", 0.2)
    assert f.busy_keys() == ["code", "cowork"]  # ≥1.0, most intense first
    assert f.total_load() == pytest.approx(3.2)


def test_capped_and_floor():
    f = PheromoneField(half_life=10_000, cap=2.0)
    f.deposit("cowork", 5.0)
    assert f.level("cowork") == pytest.approx(2.0)  # capped


# -- orchestrator integration -------------------------------------------------
def test_select_batch_legacy_without_field():
    from coworker.orchestrator import Orchestrator

    o = Orchestrator(provider=None, model="m", workspace="/tmp/x")  # type: ignore[arg-type]
    o.max_parallel = 4
    ready = [object() for _ in range(6)]
    assert len(o._select_batch(ready)) == 4  # legacy: full batch


def test_select_batch_shrinks_under_load():
    from coworker.orchestrator import Orchestrator

    o = Orchestrator(provider=None, model="m", workspace="/tmp/x")  # type: ignore[arg-type]
    o.max_parallel = 4
    ready = [object() for _ in range(6)]
    field = PheromoneField(half_life=10_000)
    # simulate 8 active tasks → load 8 ≥ max_parallel 4 → batch shrinks to 2
    for i in range(8):
        field.deposit("cowork", 1.0)
    o.pheromone = field
    assert len(o._select_batch(ready)) == 2  # 4 * (4/8)
    # a lightly loaded field keeps the full batch
    f2 = PheromoneField(half_life=10_000)
    f2.deposit("cowork", 1.0)
    o.pheromone = f2
    assert len(o._select_batch(ready)) == 4


async def test_orchestrator_deposits_and_withdraws_pheromone(tmp_path):
    from coworker.orchestrator import Orchestrator
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient

    class Scripted(ProviderClient):
        def __init__(self, turns):
            self._turns = list(turns)

        def complete(self, *, model, messages, tools=None, **settings):
            return self._turns.pop(0)

        def capabilities(self, model):
            return ModelCapabilities()

    turns = [
        AssistantTurn(text='[{"id":"t0","description":"A","deps":[]}]'),
        AssistantTurn(text="A done. " + "x" * 100, finish_reason="stop"),
        AssistantTurn(
            text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}',
            finish_reason="stop",
        ),
    ]
    field = PheromoneField(half_life=10_000)
    orch = Orchestrator(
        provider=Scripted(turns),
        model="test-model",
        workspace=str(tmp_path / "ws"),
        pheromone=field,
    )
    result = await orch.run("goal")
    assert result.status == "completed"
    # every deposit was matched by a withdraw (finally) → field is empty
    assert field.levels() == {}
    assert field.total_load() == 0.0


def test_pheromone_status_api(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    manager = SessionManager(data_dir=tmp_path / "data")
    client = TestClient(create_app(manager))
    manager.pheromone.deposit("cowork", 1.0)
    r = client.get("/v1/pheromone")
    # 真实时钟 + 60s 半衰期: 这条走完整 SessionManager/TestClient, 慢 runner 上
    # deposit→GET 之间能过去 ~0.2s, 蒸发 ~0.2%(GitHub CI run #16 实测 0.998)。
    # abs 容差容忍 ~2s 的调度停顿, 但仍能抓住真问题(丢失/错键/重度衰减)。
    assert r.json()["levels"]["cowork"] == pytest.approx(1.0, abs=0.02)
    assert r.json()["total_load"] == pytest.approx(1.0, abs=0.02)
