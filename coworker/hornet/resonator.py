"""HORNET 2D — resonator (动态共振检索).

A query is a probe wave. It seeds amplitude on the cells whose phase matches
(semantic similarity to the n-gram vector), then the wave propagates along the
hive's edges: per-hop decay from the channel (transport layer), amplified when
the neighbor's semantic phase is close to the query phase. Cells whose final
amplitude clears the floor are returned ranked, with the propagation path that
explains each hit (long-range association = resonance across the topology).

DPNN enhancement (#1): the phase gate is replaced by wave interference —
each cell has a natural frequency (Pisano period of its content fingerprint)
and the query wave has its own frequency. The interference factor
cos(Δω·t) × cos(π·d_phi) determines amplification (constructive) or
suppression (destructive). Only frequency-matched cells sustain amplitude
across multiple hops — physical resonance, not just graph decay.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .store import HornetStore, ngram_vector, cosine, coverage_similarity, CHANNEL_DECAY_3D, similarity

try:
    from .dpnn_phase import cell_omega, query_frequency, interference
    _DPNN = True
except ImportError:
    _DPNN = False


class HornetResonator:
    def __init__(
        self,
        store: HornetStore,
        *,
        seeds: int = 6,
        hops: int = 4,
        decay_floor: float = 0.05,
        amp_floor: float = 0.08,
        phase_gate: float = 0.30,
        dpnn: bool = True,
        adaptive: bool = False,  # review fix: hot-zone Euler refinement is experimental — off by default
        sync_threshold: float = 0.12,
    ) -> None:
        self.store = store
        self.seeds = seeds
        self.hops = hops
        self.decay_floor = decay_floor
        self.amp_floor = amp_floor
        self.phase_gate = phase_gate
        self.dpnn = dpnn and _DPNN
        self.adaptive = adaptive
        self.sync_threshold = sync_threshold

    def resonate(self, query: str, *, k: int = 10, hops: Optional[int] = None) -> dict[str, Any]:
        # Two-phase load (2026-08-22 crash post-mortem): the old full
        # list_nodes() materialized every cell WITH its n-gram vector — ~1.5GB
        # on a ~2.4k-cell hive — and resonate() runs on every orchestrated
        # task, which is exactly when the sidecar silently OOM-died.
        # Phase 1 keeps the light metadata the propagation/output phases touch;
        # phase 2 streams the heavy vec/content in batches and discards them.
        nodes = self.store.list_nodes(
            fields=("id", "kb_item_id", "title", "x", "y", "z", "phase")
        )
        if not nodes:
            return {"hits": [], "query_phase": [], "warnings": ["hive empty — run build first"]}
        hops = hops or self.hops
        q_vec = ngram_vector(query)
        q_phase = _query_phase(query)

        # DPNN: query probe wave frequency + precomputed cell natural frequencies.
        q_omega = query_frequency(query) if self.dpnn else 0.0
        cell_freq: dict[int, float] = {}

        # adjacency: node_id -> list of (neighbor_id, relation, channel, weight)
        adj: dict[int, list[tuple[int, str, str, float]]] = {n["id"]: [] for n in nodes}
        for e in self.store.list_edges():
            adj.setdefault(e["src"], []).append((e["dst"], e["relation"], e["channel"], e["weight"]))
            adj.setdefault(e["dst"], []).append((e["src"], e["relation"], e["channel"], e["weight"]))

        # seed amplitudes: raw query-count x doc-vector coverage (doc length
        # independent) — same retrieval spirit as the knowledge store, but on
        # whole hive cells rather than short chunks. Streamed so at most one
        # batch of vec dicts is alive at any moment.
        node_by_id = {n["id"]: n for n in nodes}
        amp: dict[int, float] = {}
        for n in self.store.iter_nodes(fields=("id", "title", "content", "vec"), batch=100):
            nid = n["id"]
            if nid not in node_by_id:  # appeared between the two loads
                continue
            s = coverage_similarity(query, n.get("vec") or {})
            if s > 0:
                amp[nid] = s
            if self.dpnn:
                cell_freq[nid] = cell_omega(n["title"], n.get("content", ""))

        # wave propagation (iterative diffusion with DPNN interference). Energy
        # is split by out-degree each hop so a dense hive doesn't explode.
        path: dict[int, list[str]] = {nid: [] for nid in amp}
        frontier = dict(amp)  # node_id -> wave energy this round
        for _hop in range(hops):
            if not frontier:
                break
            nxt: dict[int, float] = {}
            for nid, energy in frontier.items():
                if energy < self.decay_floor:
                    continue
                nbrs = adj.get(nid, [])
                share = energy * (1.0 / max(1, len(nbrs)))
                zi = node_by_id[nid].get("z", 0)
                for nbr, rel, ch, w in nbrs:
                    decay = CHANNEL_DECAY_3D.get(ch, 0.72)
                    # Cross-layer rule (spec §三层动态共振三维适配): a wave may
                    # NOT jump straight between Z+ projection and Z- traceback —
                    # it must relay through the XY fact plane, forming the
                    # "evidence → fact → projection" chain. Direct cross-zone
                    # hops are heavily damped; same-zone hops pass freely.
                    zn = node_by_id[nbr].get("z", 0)
                    if (zi > 0 and zn < 0) or (zi < 0 and zn > 0):
                        decay *= 0.30
                    contrib = share * w * decay
                    if contrib < self.decay_floor:
                        continue
                    # DPNN wave interference (#1): cos(Δω·t) × cos(π·d_phi).
                    # Frequency-matched + phase-aligned → constructive (up to 2×).
                    # Frequency-mismatched or phase-opposed → destructive (→ 0).
                    # Falls back to the hard phase_gate when dpnn is unavailable.
                    phi_n = node_by_id[nbr].get("phase") or [0.0] * 6
                    d_phi = _phase_dist(q_phase, phi_n)
                    if self.dpnn:
                        interf = interference(
                            cell_freq.get(nbr, 0.0), q_omega, _hop + 1, d_phi,
                        )
                        contrib *= 1.0 + max(interf, -0.5)  # [-0.5, 1] → [0.5×, 2×]
                    elif d_phi < self.phase_gate:
                        contrib *= 1.0 + (1.0 - d_phi / self.phase_gate)  # up to 2x
                    nxt[nbr] = nxt.get(nbr, 0.0) + contrib
            for nid, contrib in nxt.items():
                amp[nid] = amp.get(nid, 0.0) + contrib
                if nid not in path:
                    path[nid] = []
                path[nid].append(f"{rel}@{ch}")
            frontier = nxt

            # #4 adaptive precision: in hot zones (high amplitude, low neighbor
            # variance = coherent wave pattern), upgrade to continuous Euler
            # sub-steps of dA/dt = -α·A + β·L·A (graph Laplacian). This refines
            # the amplitude where resonance is actually forming, while the bulk
            # of the hive stays on the fast discrete pass.
            if self.adaptive and frontier:
                for nid in list(frontier.keys()):
                    nbrs = adj.get(nid, [])
                    if not nbrs:
                        continue
                    nbr_amps = [amp.get(nb, 0.0) for nb, _, _, _ in nbrs]
                    if not nbr_amps:
                        continue
                    mean_a = sum(nbr_amps) / len(nbr_amps)
                    var_a = sum((a - mean_a) ** 2 for a in nbr_amps) / len(nbr_amps)
                    if var_a >= self.sync_threshold ** 2:
                        continue  # not synchronized — skip continuous refinement
                    alpha = 0.72  # zone-agnostic decay
                    beta = 0.06   # diffusion coefficient
                    dt = 0.25     # sub-step size
                    for _sub in range(3):  # 3 Euler sub-steps
                        lap = sum(amp.get(nb, 0.0) - amp.get(nid, 0.0) for nb, _, _, _ in nbrs)
                        dA = -alpha * amp.get(nid, 0.0) + beta * lap
                        amp[nid] = amp.get(nid, 0.0) + dt * dA

        # rank + trim (amplitude normalized to [0,1] so the UI scale is stable)
        ranked = sorted(amp.items(), key=lambda kv: -kv[1])
        amp_max = max((a for _, a in ranked), default=1.0) or 1.0
        hits: list[dict[str, Any]] = []
        for nid, a in ranked:
            if a < self.amp_floor:
                break
            n = node_by_id[nid]
            hits.append(
                {
                    "node_id": nid,
                    "kb_item_id": n.get("kb_item_id"),  # 问题3: 关联原文知识条目
                    "title": n["title"],
                    "amplitude": round(a / amp_max, 4),
                    "path": path.get(nid, [])[-6:],
                    "x": n["x"],
                    "y": n["y"],
                    "similarity": round(amp.get(nid, 0.0) / amp_max, 4),
                }
            )
            if len(hits) >= k:
                break

        rid = self.store.record_resonance(query, hits)
        return {"run_id": rid, "hits": hits, "query_phase": q_phase}


def _query_phase(query: str) -> list[float]:
    """Coarse 12-dim semantic phase for a query (same cues as the builder)."""
    cues = {
        0: ("时间", "日期", "年", "月", "周报", "日报"),
        1: ("规则", "标准", "流程", "必须", "禁止"),
        2: ("证据", "数据", "来源", "引用", "实验"),
        3: ("属性", "特征", "参数", "尺寸", "颜色"),
        4: ("关系", "依赖", "关联", "协作"),
        5: ("实体", "名称", "模型", "系统", "软件"),
        6: ("假设", "推测", "预测", "未来", "展望"),
        7: ("规划", "计划", "方案", "步骤", "落地"),
        8: ("综合", "总结", "提炼", "范式", "框架"),
        9: ("趋势", "演变", "演进", "发展", "方向"),
        10: ("起源", "历史", "早期", "传统", "经典"),
        11: ("修订", "更新", "版本", "基线", "变更"),
    }
    phase = [0.0] * 12
    for k, kws in cues.items():
        if any(kw in query for kw in kws):
            phase[k] = 1.0
    norm = math.sqrt(sum(p * p for p in phase)) or 1.0
    return [round(p / norm, 4) for p in phase]


def _phase_dist(a: list[float], b: list[float]) -> float:
    """Phase difference in [0, ~2] (normalized 6-dim vectors)."""
    if not a or not b:
        return 1.0
    n = max(len(a), len(b))
    pa = (a + [0.0] * n)[:n]
    pb = (b + [0.0] * n)[:n]
    d = math.sqrt(sum((x - y) ** 2 for x, y in zip(pa, pb)))
    return d / 2.0  # normalized to ~[0,1]
