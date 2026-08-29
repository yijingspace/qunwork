"""QunMesh M2/M3: HexGrid 批选择 + 角色邻域化(就近评审/bft/动态领取)测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from coworker.orchestrator.mesh import (
    HexGrid,
    _mesh_mode_flags,
    algebraic_connectivity,
    bft_vote,
    claim_idle_agent,
    hex_distance,
    review_neighborhood,
    select_batch_grid,
    topology_health,
)
from coworker.orchestrator.models import ReviewVerdict, Task
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


# -- QunMesh M3: 就近评审 / swarm_bft / 动态领取 -----------------------------


def test_review_neighborhood_formats_signals():
    """result/risk 信号 → 邻域上下文文本; 无信号/None/旧场 → 空串。"""
    bus = StigmergyBus()
    bus.deposit("t1", 1.0, channel="result", payload="chapter 2 draft")
    bus.deposit("t2", 0.8, channel="risk", payload="missing citations")
    ctx = review_neighborhood(bus)
    assert "t1" in ctx and "chapter 2 draft" in ctx
    assert "t2" in ctx and "missing citations" in ctx
    assert review_neighborhood(StigmergyBus()) == ""
    assert review_neighborhood(None) == ""
    assert review_neighborhood(PheromoneField()) == ""


def test_bft_vote_unanimous_split_and_fallback():
    v_yes = ReviewVerdict(accepted=True, reason="ok", confidence=0.8)
    v_weak = ReviewVerdict(accepted=True, reason="meh", confidence=0.4)
    v_no = ReviewVerdict(accepted=False, reason="bad", confidence=0.7)
    # 一致通过
    merged, meta = bft_vote([v_yes, v_yes])
    assert merged.accepted and meta["consensus"] == 1.0 and meta["votes"] == 2
    # 分裂 2:1 → 多数通过, confidence = 通过票均值
    merged, meta = bft_vote([v_weak, v_yes, v_no])
    assert merged.accepted and merged.confidence == pytest.approx(0.6)
    assert "split" in merged.reason
    # 一致拒绝 → 拒绝 + min 置信
    merged, _meta = bft_vote([v_no, v_no])
    assert not merged.accepted and merged.confidence == pytest.approx(0.7)
    # 平票 → 保守拒绝
    merged, _meta = bft_vote([v_yes, v_no])
    assert not merged.accepted
    # needs_human 任一即真
    v_human = ReviewVerdict(accepted=True, reason="ok", confidence=0.9, needs_human=True)
    merged, _meta = bft_vote([v_human, v_no, v_yes])
    assert merged.needs_human
    # 空 → 兜底通过
    merged, meta = bft_vote([])
    assert merged.accepted and meta["votes"] == 0


def test_orchestrator_review_neighborhood_and_bft(monkeypatch):
    """mesh_review=True: prompt 注入邻域上下文; 低置信票触发 bft 3 票聚合。"""
    import coworker.orchestrator.orchestrator as orch_mod
    from coworker.orchestrator.orchestrator import Orchestrator

    bus = StigmergyBus()
    bus.deposit("t0", 1.0, channel="result", payload="earlier deliverable")
    o = Orchestrator(provider=None, model="m", workspace="w", pheromone=bus,
                     mesh_review=True)

    captured: list[str] = []

    class FakeEngine:
        pass

    async def fake_run(engine, prompt, on_event=None):
        captured.append(prompt)
        if "BFT vote" in prompt:
            return '{"accepted": true, "reason": "good", "confidence": 0.8, "needs_human": false}', "ok"
        return '{"accepted": true, "reason": "unsure", "confidence": 0.4, "needs_human": false}', "ok"

    monkeypatch.setattr(orch_mod, "build_reviewer_engine", lambda **kw: FakeEngine())
    monkeypatch.setattr(orch_mod, "_run_engine_async", fake_run)

    events: list[tuple[str, dict]] = []
    o.event_sink = lambda k, p: events.append((k, p))
    verdict = asyncio.run(
        o._review(Task(id="t9", description="write report"), "the report body")
    )
    # 低置信首票 + 2 独立票 → bft 聚合 (2 票 0.8 + 1 票 0.4)
    assert verdict.accepted
    assert verdict.confidence == pytest.approx((0.8 + 0.8 + 0.4) / 3, abs=1e-3)
    assert any(k == "review_bft" for k, _p in events)
    # 邻域上下文进了 reviewer prompt
    assert any("earlier deliverable" in p for p in captured)
    # 共 3 次评审调用 (首票 + 2 加票)
    assert len(captured) == 3


def test_claim_idle_agent_cross_role():
    """同 role 无空闲 → 跨 role 低负载领取; 全忙 → (None, None); 异常安全。"""

    class FakeAgent:
        def __init__(self, rid, role, load, available=True):
            self.id, self.role, self.load, self.is_available = rid, role, load, available
            self.created_at = 0.0

    class FakePool:
        def __init__(self, agents):
            self._agents = agents

        def list(self):
            return list(self._agents)

        def acquire(self, role, **kw):
            for a in self._agents:
                if a.role == role and a.is_available:
                    a.is_available = False
                    return a
            return None

    pool = FakePool([
        FakeAgent("a-code", "code", 0.0, available=True),
        FakeAgent("a-rev", "reviewer", 0.6, available=True),
    ])
    inst, ev = claim_idle_agent(pool, "cowork", task_id="t1")
    assert inst is not None and inst.id == "a-code"  # 低负载优先
    assert ev["claimed_role"] == "code" and ev["want_role"] == "cowork"
    # 剩余空闲实例继续可领 (跨 role 持续领取)
    inst2, ev2 = claim_idle_agent(pool, "cowork", task_id="t2")
    assert inst2 is not None and inst2.id == "a-rev"
    # 全忙 → (None, None)
    for a in pool.list():
        a.is_available = False
    assert claim_idle_agent(pool, "cowork", task_id="t3") == (None, None)
    # 异常安全 (list 抛错)
    class BoomPool:
        def list(self):
            raise RuntimeError("boom")

    assert claim_idle_agent(BoomPool(), "cowork", task_id="t4") == (None, None)


# -- QunMesh M4: mesh_mode 四档 / λ₂ 拓扑遥测 / 热点迁徙 ---------------------


def test_mesh_mode_flags_four_positions():
    from coworker.orchestrator.mesh import _mesh_mode_flags

    assert _mesh_mode_flags("off") == {"scheduling": False, "claim": False, "review": False}
    assert _mesh_mode_flags("serial") == {"scheduling": False, "claim": False, "review": False}
    assert _mesh_mode_flags("hybrid") == {"scheduling": True, "claim": True, "review": False}
    assert _mesh_mode_flags("full") == {"scheduling": True, "claim": True, "review": True}
    # 非法值 / None / 大小写 → off 兜底
    assert _mesh_mode_flags("FULL")["review"] is True
    assert _mesh_mode_flags("bogus")["scheduling"] is False
    assert _mesh_mode_flags(None)["scheduling"] is False


def test_hexgrid_spiral_compactness():
    """修复回归: 螺旋放置必须紧凑 (ring k 全部距中心 k), 中心 agent 6 邻居。"""
    cells = HexGrid._spiral(2)
    dists = [hex_distance((0, 0), c) for c in cells]
    assert dists[:7] == [0] + [1] * 6  # 中心 + ring1
    assert dists[7:] == [2] * 12  # ring2
    g = HexGrid(radius=4)
    for name in ("a", "b", "c", "d", "e", "f", "g"):
        g.place(name)
    assert len(g.neighbors("a", ring=1)) == 6  # 中心恰好六邻接


def test_algebraic_connectivity_known_graphs():
    """λ₂ 已知图验证: 双节点单边 λ₂=2; 六邻接星形 (中心+6邻居) λ₂>0; 单节点 0。"""
    g = HexGrid(radius=4)
    g.place("a")
    assert algebraic_connectivity(g, ["a"]) == 0.0
    g.place("b")
    # a(0,0) b(ring1) — 单边 Laplacian 特征值 {0, 2}
    assert algebraic_connectivity(g, ["a", "b"]) == pytest.approx(2.0)
    for name in ("c", "d", "e", "f", "h"):
        g.place(name)
    lam = algebraic_connectivity(g, ["a", "b", "c", "d", "e", "f", "h"])
    assert 0.0 < lam < 6.0  # 连通且非完全图


def test_topology_health_hotspot_and_migration():
    """热点检测: load ≥ floor 且 > 邻居均 × ratio; 迁徙目标 = 邻居最低负载。"""
    bus = StigmergyBus()
    bus.deposit("hub", 3.0)  # 热点
    bus.deposit("nb1", 0.5)
    bus.deposit("nb2", 0.2)  # 邻居最低 → 迁徙目标
    health = topology_health(bus)
    assert health["hotspots"] == ["hub"]
    assert len(health["migrations"]) == 1
    m = health["migrations"][0]
    assert m["hotspot"] == "hub" and m["target"] in ("nb1", "nb2")
    assert m["target"] == "nb2"  # 0.2 < 0.5
    assert health["lambda2"] > 0.0 and health["edges"] > 0
    # 均匀负载 → 无热点
    bus2 = StigmergyBus()
    for a in ("x", "y"):
        bus2.deposit(a, 1.0)
    assert topology_health(bus2)["hotspots"] == []
    # None / 干净场 → 空骨架
    empty = topology_health(StigmergyBus())
    assert empty["agents"] == [] and empty["lambda2"] == 0.0


def test_orchestrator_mesh_mode_post_init():
    """__post_init__: mode 推导三开关 (与显式 bool 取或) + full 启拓扑遥测。"""
    from coworker.orchestrator.orchestrator import Orchestrator

    o = Orchestrator(provider=None, model="m", workspace="w", mesh_mode="full")
    assert o.mesh_scheduling and o.mesh_claim and o.mesh_review
    assert o._mesh_topology_enabled is True

    o2 = Orchestrator(provider=None, model="m", workspace="w", mesh_mode="hybrid")
    assert o2.mesh_scheduling and o2.mesh_claim and not o2.mesh_review
    assert o2._mesh_topology_enabled is False

    o3 = Orchestrator(provider=None, model="m", workspace="w", mesh_mode="off")
    assert not o3.mesh_scheduling and not o3._mesh_topology_enabled

    # 显式 bool 与 mode 取或 (serial 不开任何能力, 显式 True 保留)
    o4 = Orchestrator(provider=None, model="m", workspace="w",
                      mesh_mode="serial", mesh_review=True)
    assert o4.mesh_review and not o4.mesh_scheduling and not o4.mesh_claim

    o5 = Orchestrator(provider=None, model="m", workspace="w", mesh_mode="off",
                      mesh_scheduling=True)
    assert o5.mesh_scheduling and not o5._mesh_topology_enabled


def test_pheromone_bus_channels_summary_shape():
    """channels_summary 四信道形状 (pheromone_status 的 channels 字段)。"""
    bus = StigmergyBus()
    bus.deposit("t1", 1.0, channel="task")
    s = bus.channels_summary()
    assert set(s) == {"load", "task", "result", "risk"}
    assert s["task"]["signals"] == 1.0
