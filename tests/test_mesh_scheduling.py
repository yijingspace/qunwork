"""QunMesh M2: HexGrid 邻域感知批选择 + orchestrator 接线测试。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from coworker.orchestrator.mesh import HexGrid, hex_distance, select_batch_grid
from coworker.pheromone import PheromoneField, StigmergyBus


@dataclass
class FakeTask:
    id: str
    agent: str = ""
    deps: list = field(default_factory=list)


# -- HexGrid ----------------------------------------------------------------


def test_hexgrid_place_idempotent_and_neighbors():
    g = HexGrid(radius=3)
    p1 = g.place("a")
    p2 = g.place("a")
    assert p1 == p2  # 幂等
    for name in ("b", "c", "d", "e", "f", "h", "i", "j", "k"):
        g.place(name)
    # 中心 a 的 ring=1 邻接是距离 1 的格子 (螺旋首环)
    nb = g.neighbors("a", ring=1)
    assert 1 <= len(nb) <= 6
    assert "a" not in nb
    assert hex_distance((0, 0), (2, -1)) == 2


def test_hexgrid_spread_score():
    g = HexGrid(radius=4)
    for name in ("a", "b", "c"):
        g.place(name)
    assert g.spread_score(["a"]) == 0.0
    assert g.spread_score(["a", "b", "c"]) > 0.0


# -- select_batch_grid ------------------------------------------------------


def test_batch_gradient_prefers_idle_agents():
    """忙 agent (load=1) 的任务排后, 空闲 agent 任务先进批。"""
    bus = StigmergyBus()
    bus.deposit("cowork", 1.0)  # 热点在跑
    ready = [
        FakeTask("t-busy-1", agent="cowork"),
        FakeTask("t-idle-1", agent="code"),
        FakeTask("t-busy-2", agent="cowork"),
        FakeTask("t-idle-2", agent="review-a"),
    ]
    batch = select_batch_grid(ready, bus, 2, grid=HexGrid())
    ids = [t.id for t in batch]
    assert "t-idle-1" in ids and "t-idle-2" in ids  # 空闲邻域优先


def test_batch_spread_cap_limits_hotspot():
    """≥2 agent 时同 agent 批内名额 ≤ ceil(n*0.5) — 热点不占满批。"""
    bus = StigmergyBus()
    ready = [FakeTask(f"t{i}", agent="cowork") for i in range(4)]
    ready += [FakeTask("x1", agent="code"), FakeTask("x2", agent="review-a")]
    batch = select_batch_grid(ready, bus, 4, grid=HexGrid())
    from collections import Counter

    cnt = Counter(t.agent for t in batch)
    assert cnt["cowork"] <= 2  # cap = ceil(4*0.5)


def test_batch_single_agent_falls_back_to_order():
    """全同 agent → 退回原序截断 (无梯度可读)。"""
    bus = StigmergyBus()
    ready = [FakeTask(f"t{i}", agent="cowork") for i in range(6)]
    batch = select_batch_grid(ready, bus, 3, grid=HexGrid())
    assert [t.id for t in batch] == ["t0", "t1", "t2"]


def test_batch_no_pheromone_returns_truncation():
    ready = [FakeTask(f"t{i}") for i in range(5)]
    assert [t.id for t in select_batch_grid(ready, None, 3)] == ["t0", "t1", "t2"]
    # PheromoneField (旧场) 也走兼容路径
    ready2 = [FakeTask(f"t{i}") for i in range(5)]
    out = select_batch_grid(ready2, PheromoneField(), 3, grid=HexGrid())
    assert len(out) == 3


# -- orchestrator 接线 -------------------------------------------------------


def test_orchestrator_mesh_flag_select_batch():
    """mesh_scheduling=True → _select_batch 走网格路径 (梯度优先空闲邻域)。"""
    from coworker.orchestrator.orchestrator import Orchestrator

    bus = StigmergyBus()
    bus.deposit("cowork", 1.0)
    orch = Orchestrator(provider=None, model="m", workspace="w", pheromone=bus,
                        mesh_scheduling=True, max_parallel=2)
    ready = [FakeTask("t1", agent="code"), FakeTask("t2", agent="cowork")]
    batch = orch._select_batch(ready)
    assert batch[0].id == "t1"  # 忙 agent 的任务被梯度排后

    orch_legacy = Orchestrator(provider=None, model="m", workspace="w", pheromone=bus,
                               mesh_scheduling=False, max_parallel=2)
    assert [t.id for t in orch_legacy._select_batch(ready)] == ["t1", "t2"]


def test_orchestrator_pher_deposit_channel_safety():
    """_pher_deposit: StigmergyBus 四信道写入; PheromoneField 场景静默 no-op;
    异常不外泄。"""
    from coworker.orchestrator.orchestrator import Orchestrator

    bus = StigmergyBus()
    orch = Orchestrator(provider=None, model="m", workspace="w", pheromone=bus)
    orch._pher_deposit("t1", 1.0, channel="task", payload="desc")
    assert bus.level("t1", channel="task") == pytest.approx(1.0)
    assert bus.payload_of("t1", channel="task") == "desc"

    orch_field = Orchestrator(provider=None, model="m", workspace="w",
                              pheromone=PheromoneField())
    orch_field._pher_deposit("t1", 1.0, channel="result")  # no-op, 不抛
    orch_none = Orchestrator(provider=None, model="m", workspace="w", pheromone=None)
    orch_none._pher_deposit("t1", 1.0, channel="risk")  # no-op, 不抛


def test_pheromone_bus_channels_summary_shape():
    """channels_summary 四信道形状 (pheromone_status 的 channels 字段)。"""
    bus = StigmergyBus()
    bus.deposit("t1", 1.0, channel="task")
    s = bus.channels_summary()
    assert set(s) == {"load", "task", "result", "risk"}
    assert s["task"]["signals"] == 1.0
