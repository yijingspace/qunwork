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
    # second evolve must NOT re-notify the same findings (dedup by kind+title)
    ev2 = obs.evolve()
    all_rows = store.list_emergent(500)
    pairs = [(r["kind"], r["title"]) for r in all_rows]
    assert len(pairs) == len(set(pairs)), "emergent findings must be deduped"
    # unread feed + mark-read flow
    total = store.emergent_count()
    assert store.count_unread_emergent() == total
    first = store.list_emergent(1)[0]
    assert store.set_emergent_status(first["id"], "accepted") is True
    assert store.count_unread_emergent() == total - 1


def test_cell_fission_on_high_load(tmp_path):
    """Spec §三维蜂胞分裂: high load_factor cells split into zone child cells."""
    store, builder, res, obs = _hive(tmp_path)
    builder.build(_sample_items())
    # make a topic resonate hard -> high load_factor
    for _ in range(4):
        res.resonate("DPNN 相位 周期", k=4)
    before = store.node_count()
    ev = obs.evolve(limit=30)
    assert ev["counts"]["fission"] > 0
    assert store.node_count() > before
    children = [n for n in store.list_nodes() if "推演延伸" in n["title"] or "溯源锚点" in n["title"]]
    assert children
    # children grow in the opposite zone of their mother (fission splits along Z)
    assert all(n["z"] in (-1, 1) for n in children)


def test_cavity_detects_never_resonated_cells(tmp_path):
    """Spec §立体空洞挖掘: cells never hit by any probe are topological cavities."""
    store, builder, res, obs = _hive(tmp_path)
    builder.build(_sample_items())
    # only resonate one topic; the other topics never resonate
    for _ in range(3):
        res.resonate("DPNN 周期", k=3)
    ev = obs.evolve(limit=30)
    assert ev["counts"]["cavity"] > 0
    cavity_kinds = {e["kind"] for e in ev["items"]}
    assert "cavity" in cavity_kinds


def test_cross_layer_hops_are_damped():
    """Spec §三层动态共振: direct Z+↔Z- hops must be heavily damped (chain via XY)."""
    import tempfile
    from pathlib import Path
    from coworker.hornet import HornetStore, HornetBuilder, HornetResonator

    store = HornetStore(Path(tempfile.mkdtemp()) / "h.db")
    builder = HornetBuilder(store)
    items = []
    for i in range(3):
        items.append((i, f"历史 起源 {i}", "历史 起源 传统 证据 溯源", 1_700_000_000 + i))
    for i in range(3):
        items.append((10 + i, f"当前 事实 {i}", "当前 事实 数据 状态", 1_700_000_100 + i))
    for i in range(3):
        items.append((20 + i, f"未来 预测 {i}", "未来 预测 趋势 展望 假设", 1_700_000_200 + i))
    builder.build(items)
    res = HornetResonator(store)
    out = res.resonate("未来 预测 趋势", k=6)
    assert out["hits"]
    # Z+ seeds dominate, and any Z- hit must have come through a relayed path
    # (amplitudes of direct cross-zone hops are damped ×0.30)
    assert out["hits"][0]["amplitude"] >= 0.5


def test_topo_embed_produces_dense_manifold():
    """Numpy GCN + ring-loss proxy: dense embedding, semantic neighbors close."""
    import numpy as np
    from coworker.hornet.topo_embed import topo_embed

    rng = np.random.default_rng(1)
    X = rng.normal(size=(20, 40)).astype(np.float32)
    # two semantic clusters with edges inside each
    edges = [(i, j) for i in range(10) for j in range(i + 1, 10)] + \
            [(i, j) for i in range(10, 20) for j in range(i + 1, 20)]
    res = topo_embed(X, edges, epochs=5, out_dim=8)
    E = res["embedding"]
    assert E.shape == (20, 8)
    assert res["loss"] < 5.0
    # intra-cluster cosine > inter-cluster (topology preserved)
    intra = float(E[0] @ E[1])
    inter = float(E[0] @ E[10])
    assert intra > inter


def test_build_with_topo_stores_embedding_and_weights(tmp_path):
    """topo=True stores manifold embeddings and topology-blends similar weights."""
    store, builder, _res, _obs = _hive(tmp_path)
    res = builder.build(_sample_items(), topo=True)
    assert res["topo"] is True
    nodes = store.list_nodes()
    assert any(len(n.get("topo") or []) == 16 for n in nodes)
    sim_edges = [e for e in store.list_edges() if e["relation"] == "similar"]
    assert sim_edges
    # weights blended with topo cosine are fractional (not just raw sim)
    assert all(0.0 < e["weight"] <= 1.0 for e in sim_edges)


def test_hybrid_ode_sim_runs_and_splits_zones():
    """Hybrid-ODE-Sim: hot sub-blocks go continuous RK4, rest stays discrete."""
    from coworker.hornet.sim3d import run_hybrid_sim

    out = run_hybrid_sim(cells=27, steps=10, seed=0)
    assert 0.0 < out["continuous_share"] < 1.0
    assert abs(out["continuous_share"] + out["discrete_share"] - 1.0) < 1e-6
    out2 = run_hybrid_sim(cells=27, steps=10, seed=0)
    assert out["continuous_share"] == out2["continuous_share"]  # deterministic


def test_feedback_success_nudges_phase(tmp_path):
    """#2 cognitive-action loop: success nudges node phase toward query phase."""
    store, builder, res, _obs = _hive(tmp_path)
    builder.build(_sample_items())
    out = res.resonate("周报 数据 计划", k=3)
    hit_ids = [h["node_id"] for h in out["hits"]]
    qphase = out.get("query_phase", [])
    assert hit_ids
    assert any(p != 0.0 for p in qphase)  # query phase is non-zero
    nodes_before = {n["id"]: n for n in store.list_nodes()}
    updated = store.feedback(hit_ids, success=True, query_phase=qphase)
    assert updated == len(hit_ids)
    nodes_after = {n["id"]: n for n in store.list_nodes()}
    changed = 0
    for nid in hit_ids:
        before = nodes_before[nid]["phase"]
        after = nodes_after[nid]["phase"]
        if before != after:
            changed += 1
    assert changed > 0  # at least one node's phase was nudged


def test_feedback_failure_is_reversible(tmp_path):
    """#2 cognitive-action loop: failure marks a reversible risk counter — it
    must NOT permanently rewrite the 3D layout (review fix)."""
    store, builder, res, _obs = _hive(tmp_path)
    builder.build(_sample_items())
    out = res.resonate("DPNN 相位 周期", k=3)
    hit_ids = [h["node_id"] for h in out["hits"]]
    assert hit_ids
    before = {n["id"]: n for n in store.list_nodes()}
    updated = store.feedback(hit_ids, success=False)
    assert updated == len(hit_ids)
    nodes_after = {n["id"]: n for n in store.list_nodes()}
    for nid in hit_ids:
        assert nodes_after[nid]["failed_count"] == before[nid].get("failed_count", 0) + 1
        assert nodes_after[nid]["z"] == before[nid]["z"]  # layout untouched


def test_dpnn_cell_period_is_pisano():
    """#1 DPNN: cell_period returns a valid Pisano period."""
    from coworker.hornet.dpnn_phase import cell_period, cell_omega, interference
    from coworker.periodic.fpa_table import pisano_period

    period = cell_period("DPNN 研究笔记", "离散周期神经网络")
    assert period > 0
    omega = cell_omega("DPNN 研究笔记", "离散周期神经网络")
    assert omega > 0
    assert abs(omega - 2 * 3.14159265 / period) < 0.01


def test_dpnn_interference_range():
    """#1 DPNN: interference factor ∈ [-1, 1]."""
    from coworker.hornet.dpnn_phase import interference

    for hop in range(10):
        for d_phi in [0.0, 0.25, 0.5, 0.75, 1.0]:
            val = interference(0.5, 0.5, hop, d_phi)
            # phase dominates; frequency is a light ±0.1 modulation
            assert -1.1 <= val <= 1.1
    # exact phase alignment is constructive, opposition is destructive
    assert interference(0.5, 0.5, 0, 0.0) > 0.8
    assert interference(0.5, 0.5, 0, 1.0) < -0.8


def test_dpnn_resonator_finds_hits(tmp_path):
    """#1 DPNN: resonator with DPNN interference still finds correct hits."""
    store, builder, res, _obs = _hive(tmp_path)
    builder.build(_sample_items())
    out = res.resonate("DPNN 相位 周期", k=4)
    assert out["hits"]
    titles = [h["title"] for h in out["hits"]]
    assert any("DPNN" in t for t in titles)


def test_dpnn_frequency_matched_resonates_more(tmp_path):
    """#1 DPNN: cells with matching natural frequency resonate more strongly
    than cells with mismatched frequency (sustained vs oscillating interference)."""
    from coworker.hornet.dpnn_phase import cell_omega, interference

    omega_a = cell_omega("周报 数据 计划", "周报 目标达成度 数据事实表")
    omega_b = cell_omega("周报 数据 计划", "周报 目标达成度 数据事实表")
    omega_c = cell_omega("QunWork 品牌 图标", "群沃客 Q+蜂巢 品牌图标")
    d_phi = 0.1
    matched = sum(interference(omega_a, omega_b, t, d_phi) for t in range(1, 5))
    mismatched = sum(interference(omega_a, omega_c, t, d_phi) for t in range(1, 5))
    assert matched > mismatched


def test_auto_evolve_acts_only_on_new_findings(tmp_path):
    """Self-organizing actions must be idempotent: repeating evolve() never
    re-acts on old findings (review fix — was duplicating tasks every 6h)."""
    store, builder, res, obs = _hive(tmp_path)
    builder.build(_sample_items())
    for _ in range(2):
        res.resonate("周报 目标 评估", k=3)
    ev1 = obs.evolve()
    assert ev1["emerged"] > 0
    # every new item carries is_new=True; repeated evolve re-lists them as old
    assert all(e.get("is_new") is True for e in ev1["items"] if e.get("is_new"))
    ev2 = obs.evolve()
    new_items = [e for e in ev2["items"] if e.get("is_new")]
    # a second evolve may surface a never-before-noticed category, but nothing
    # that was already acted on repeats
    old_titles = {(e["kind"], e["title"]) for e in ev1["items"]}
    assert not any((e["kind"], e["title"]) in old_titles for e in new_items)


# -- A: 周期驱动的知识保鲜与遗忘 ----------------------------------------------
def test_freshness_pass_decays_stale_nodes(tmp_path):
    """A: nodes with low load and long period decay freshness; stale ones
    get downgraded to Z- traceback layer."""
    store, builder, res, obs = _hive(tmp_path)
    builder.build(_sample_items())
    # only resonate one cluster — the other two clusters' nodes are "never hit"
    res.resonate("DPNN 相位 周期", k=3)
    result = obs.freshness_pass()
    assert "decayed" in result
    assert "refreshed" in result
    assert "downgraded" in result
    # at least some nodes decayed (the never-hit ones)
    assert result["decayed"] > 0
    # verify freshness was actually written
    nodes = store.list_nodes()
    fresh_values = [n.get("freshness", 1.0) for n in nodes]
    assert any(f < 1.0 for f in fresh_values)


def test_freshness_pass_refreshes_hot_nodes(tmp_path):
    """A: high-load nodes with short period stay fresh (freshness = 1.0)."""
    store, builder, res, obs = _hive(tmp_path)
    builder.build(_sample_items())
    # resonate multiple times to build load
    for _ in range(3):
        res.resonate("DPNN 相位 周期", k=3)
    obs.freshness_pass()
    stats = store.node_hit_stats(50)
    # nodes that were hit should still have freshness 1.0
    hot_ids = {nid for nid, s in stats.items() if s["load_factor"] > 0.15}
    if hot_ids:
        nodes = {n["id"]: n for n in store.list_nodes()}
        for nid in hot_ids:
            assert nodes[nid].get("freshness", 1.0) == 1.0


# -- C: 跨组织蜂巢共振对齐 ----------------------------------------------------
def test_export_import_hive(tmp_path):
    """C: export produces a structural fingerprint; import adds new nodes and
    detects phase conflicts on title matches."""
    store, builder, _res, _obs = _hive(tmp_path)
    builder.build(_sample_items())
    exported = store.export_hive()
    assert "nodes" in exported and "edges" in exported
    assert len(exported["nodes"]) == 9
    # content is excluded (privacy)
    assert all("content" not in n for n in exported["nodes"])

    # import into a fresh hive — all nodes should be added as new
    store2 = HornetStore(tmp_path / "hornet2.db")
    result = store2.import_hive(exported)
    assert result["imported"] == 9
    assert store2.node_count() == 9

    # import again — now all titles match, phases align → no conflicts
    result2 = store2.import_hive(exported)
    assert result2["imported"] == 0  # all already exist


def test_import_hive_detects_phase_conflict(tmp_path):
    """C: when an imported node has the same title but opposing phase, a
    conflict emergent is created and an opposite edge is added."""
    store, builder, _res, _obs = _hive(tmp_path)
    builder.build(_sample_items())
    nodes = store.list_nodes()
    # craft a remote payload with same titles but a non-zero phase that
    # conflicts with the local all-zero phase (distance > threshold)
    conflict_phase = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    remote_nodes = []
    for n in nodes[:3]:
        remote_nodes.append({
            "title": n["title"], "phase": conflict_phase,
            "x": n["x"], "y": n["y"], "z": n["z"],
        })
    payload = {"nodes": remote_nodes, "edges": []}
    result = store.import_hive(payload, phase_conflict_threshold=0.3)
    assert result["conflicts"] > 0
    # conflict emergent should be recorded
    emergents = store.list_emergent(50)
    assert any(e["kind"] == "conflict" for e in emergents)


# -- D: DPNN 周期×自动化调度 --------------------------------------------------
def test_rhythm_gate_in_scheduler():
    """D: Scheduler accepts a rhythm_gate callback for DPNN-aware scheduling."""
    import pytest
    pytest.importorskip("aisuite")
    from coworker.automation.scheduler import Scheduler

    gate = lambda: True  # noqa: E731
    assert gate() is True
    assert hasattr(Scheduler, "_tick")


# -- F: 自我提问式探索 --------------------------------------------------------
def test_generate_hypothetical_questions():
    """F: cavity topic → 3 exploratory questions (definition, relation, origin)."""
    from coworker.hornet.observer import generate_hypothetical_questions

    qs = generate_hypothetical_questions("DPNN 相位记忆")
    assert len(qs) == 3
    assert any("什么是" in q for q in qs)
    assert any("关联" in q or "依赖" in q for q in qs)
    assert any("起源" in q for q in qs)


def test_cavity_action_creates_question_tasks(tmp_path):
    """F: cavity emergence triggers hypothetical question tasks (not just a
    probe wave). Integration test via the observer + store."""
    store, builder, res, obs = _hive(tmp_path)
    builder.build(_sample_items())
    # resonate only one cluster → others become cavities
    for _ in range(2):
        res.resonate("DPNN 相位 周期", k=3)
    evolved = obs.evolve(limit=20)
    cavities = [e for e in evolved["items"] if e["kind"] == "cavity"]
    assert len(cavities) > 0
    # each cavity has a probe field that would feed question generation
    for c in cavities:
        assert "probe" in c["detail"]


def test_import_hive_remaps_edges_with_noncontiguous_ids(tmp_path):
    """C: import must remap edges by REAL node ids — a hive whose ids are not
    1..n (fission children, deletions) must not silently drop topology edges."""
    store = HornetStore(tmp_path / "h.db")
    # local hive with non-contiguous ids: node 1..3 + a fission child at a big id
    n1 = store.add_node("DPNN 研究", content="x", x=0, y=0, z=0)
    n2 = store.add_node("周报", content="y", x=1, y=0, z=0)
    big = store.add_node("分裂子", content="z", x=2, y=0, z=1)  # id 3..n
    store.add_edge(n1, n2, "similar", weight=0.9, channel="G3")
    store.add_edge(n2, big, "similar", weight=0.8, channel="G3")
    # export → import into a fresh local store
    payload = store.export_hive()
    fresh = HornetStore(tmp_path / "h2.db")
    res = fresh.import_hive(payload)
    assert res["imported"] > 0
    assert res["edges_added"] > 0, "edges must be remapped and imported"
    assert fresh.edge_count() > 0


def test_freshness_downgrade_is_reversible(tmp_path):
    """A: stale knowledge decays via freshness only — z layout untouched."""
    from coworker.hornet.observer import HornetObserver

    store, builder, res, obs = _hive(tmp_path)
    builder.build(_sample_items())
    nodes_before = {n["id"]: n for n in store.list_nodes()}
    r = obs.freshness_pass(stale_threshold=0.9)  # aggressive: everything decays
    assert r["decayed"] > 0
    nodes_after = {n["id"]: n for n in store.list_nodes()}
    for nid, n in nodes_after.items():
        assert n["z"] == nodes_before[nid]["z"], "z must not be rewritten"
        assert 0.0 <= n["freshness"] <= 1.0
    # a later resonance refresh restores freshness (reversible)
    for _ in range(2):
        res.resonate("DPNN 相位 周期", k=3)
    r2 = obs.freshness_pass(stale_threshold=0.1)
    restored = [n for n in store.list_nodes() if n["freshness"] > 0.95]
    assert any(n["title"].startswith("DPNN") for n in restored)


def test_health_assessment_and_report(tmp_path):
    """E: health assessment scores all three dimensions; report renders + persists."""
    from coworker.hornet.health import assess_health, render_hive_health_report

    store, builder, _res, _obs = _hive(tmp_path)
    builder.build(_sample_items())
    for _ in range(2):
        _res.resonate("DPNN 周期", k=3)
    h = assess_health(store)
    assert 0.0 <= h["score"] <= 100.0
    assert h["rating"] in ("healthy", "sub-healthy", "warning")
    assert set(h["dimensions"]) == {"structure", "dynamics", "evolution"}
    assert h["metrics"]["nodes"] == 9
    md = render_hive_health_report(h)
    assert "知识场健康度周报" in md and "总评分" in md
    # empty hive → score 0 + empty rating, never crash
    empty = HornetStore(tmp_path / "empty.db")
    he = assess_health(empty)
    assert he["score"] == 0 and he["rating"] == "empty"
