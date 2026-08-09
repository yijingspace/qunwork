# -*- coding: utf-8 -*-
"""HORNET (蜂巢共振神经拓扑) 2D layer tests."""
from pathlib import Path

from coworker.hornet import HornetBuilder, HornetObserver, HornetResonator, HornetStore


def _sample_items() -> list[tuple]:
    items = []
    for i in range(3):
        items.append((i, f"DPNN 研究笔记 {i}", "离散周期神经网络 DPNN 相位记忆 皮萨诺周期 共振", 1700000000 + i * 100))
    for i in range(3):
        items.append((10 + i, f"周报 2026W3{i} 目标达成", "周报 目标达成度 数据事实表 下周计划 评估", 1700000100 + i * 100))
    for i in range(3):
        items.append((20 + i, f"QunWork 品牌 {i}", "群沃客 Q+蜂巢 让协作自然发生 品牌图标", 1700000200 + i * 100))
    return items


def _hive(tmp_path: Path):
    store = HornetStore(tmp_path / "hornet.db")
    return store, HornetBuilder(store), HornetResonator(store), HornetObserver(store)


def test_build_creates_nodes_edges_and_hex_coords(tmp_path):
    store, builder, _res, _obs = _hive(tmp_path)
    res = builder.build(_sample_items())
    assert res["nodes"] == 9
    assert res["edges"] >= 6  # similar clusters inside each topic
    nodes = store.list_nodes()
    assert len(nodes) == 9
    coords = {(n["x"], n["y"], n["z"]) for n in nodes}
    assert len(coords) == 9  # no two cells share a 3D lattice tile
    # topic clusters share similar edges
    rels = {e["relation"] for e in store.list_edges()}
    assert "similar" in rels


def test_resonate_finds_topic_and_explains_path(tmp_path):
    store, builder, res, _obs = _hive(tmp_path)
    builder.build(_sample_items())
    out = res.resonate("DPNN 相位 周期", k=4)
    assert out["hits"]
    titles = [h["title"] for h in out["hits"]]
    assert all("DPNN" in t for t in titles)
    assert out["hits"][0]["path"]  # propagation path present
    # resonance is recorded
    assert store.recent_resonance(10)


def test_resonate_empty_hive_warns(tmp_path):
    store, _b, res, _obs = _hive(tmp_path)
    out = res.resonate("anything", k=3)
    assert out["hits"] == []
    assert any("build" in w for w in out.get("warnings", []))


def test_evolve_detects_hypernodes_from_co_resonance(tmp_path):
    store, builder, res, obs = _hive(tmp_path)
    builder.build(_sample_items())
    for _ in range(3):
        res.resonate("周报 目标 评估", k=3)
    ev = obs.evolve()
    assert ev["emerged"] > 0
    kinds = {e["kind"] for e in ev["items"]}
    assert "hypernode" in kinds
    assert store.emergent_count() > 0
    # status toggle
    first = store.list_emergent(1)[0]
    assert store.set_emergent_status(first["id"], "accepted") is True


def test_similarity_helpers():
    from coworker.hornet.store import cosine, ngram_vector, similarity

    a = "离散周期神经网络 DPNN 相位记忆"
    b = "DPNN 相位记忆 皮萨诺周期"
    assert similarity(a, b) > similarity(a, "群沃客 品牌 图标 协作")
    assert 0.0 <= cosine(ngram_vector(a), ngram_vector(b)) <= 1.0


def test_resonance_spreads_across_similar_cluster(tmp_path):
    """Long-range resonance: a query seeding one topic also surfaces its similar
    neighbors via the D5 similar channel (amplitude normalized 0..1)."""
    store, builder, res, _obs = _hive(tmp_path)
    builder.build(_sample_items())
    out = res.resonate("DPNN 周期", k=6)
    assert out["hits"]
    assert 0.0 <= out["hits"][0]["amplitude"] <= 1.0
    titles = [h["title"] for h in out["hits"]]
    # DPNN seeds hit, and the similar chain reaches other DPNN cells
    assert any("DPNN" in t for t in titles)
    assert any(h["path"] for h in out["hits"])  # propagation paths explained


def test_3d_channels_and_phase_dimensions(tmp_path):
    """3D upgrade: 12-dim phases, G-channel edges, z layers present."""
    store, builder, res, _obs = _hive(tmp_path)
    builder.build(_sample_items())
    nodes = store.list_nodes()
    assert all(len(n["phase"]) == 12 for n in nodes)
    assert len({n["z"] for n in nodes}) >= 1
    edges = store.list_edges()
    assert edges
    # edge channels are 3D G-channels
    assert all(e["channel"].startswith("G") for e in edges)


def test_zone_decay_order():
    """Z+ diffuses fast, Z- converges hard — per spec §三维 zone 衰减."""
    from coworker.hornet.store import CHANNEL_DECAY_3D

    assert CHANNEL_DECAY_3D["G4"] < CHANNEL_DECAY_3D["G0"] < CHANNEL_DECAY_3D["G8"]
    # z- traceback is the strongest convergence
    assert CHANNEL_DECAY_3D["G8"] > CHANNEL_DECAY_3D["G4"]


def test_sim3d_runs_and_finds_period():
    """Research module: deterministic, FFT yields a finite dominant period."""
    from coworker.hornet.sim3d import run_simulation

    out = run_simulation(cells=27, steps=30, seed=0, freq=0.25)
    assert out["channels"] == 12
    assert out["dominant_period"] > 0
    assert set(out["energy_by_zone"]) == {"xy", "z+", "z-"}
    # reproducibility
    out2 = run_simulation(cells=27, steps=30, seed=0, freq=0.25)
    assert out["dominant_period"] == out2["dominant_period"]


def test_emergence_dedup_and_unread_flow(tmp_path):
    """Auto-evolve surfaces new findings once; humans mark them read."""
    store, builder, res, obs = _hive(tmp_path)
    builder.build(_sample_items())
    for _ in range(2):
        res.resonate("周报 目标 评估", k=3)
    ev1 = obs.evolve()
    assert ev1["emerged"] > 0
    n1 = store.emergent_count()
    # second evolve must NOT re-notify the same findings (dedup)
    ev2 = obs.evolve()
    assert store.emergent_count() == n1
    assert ev2["emerged"] == 0
    # unread feed + mark-read flow
    assert store.count_unread_emergent() == n1
    first = store.list_emergent(1)[0]
    assert store.set_emergent_status(first["id"], "accepted") is True
    assert store.count_unread_emergent() == n1 - 1
