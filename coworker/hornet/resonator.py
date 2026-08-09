"""HORNET 2D — resonator (动态共振检索).

A query is a probe wave. It seeds amplitude on the cells whose phase matches
(semantic similarity to the n-gram vector), then the wave propagates along the
hive's edges: per-hop decay from the channel (transport layer), amplified when
the neighbor's semantic phase is close to the query phase. Cells whose final
amplitude clears the floor are returned ranked, with the propagation path that
explains each hit (long-range association = resonance across the topology).
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .store import HornetStore, ngram_vector, cosine, CHANNEL_DECAY, similarity


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
    ) -> None:
        self.store = store
        self.seeds = seeds
        self.hops = hops
        self.decay_floor = decay_floor
        self.amp_floor = amp_floor
        self.phase_gate = phase_gate

    def resonate(self, query: str, *, k: int = 10, hops: Optional[int] = None) -> dict[str, Any]:
        nodes = self.store.list_nodes()
        if not nodes:
            return {"hits": [], "query_phase": [], "warnings": ["hive empty — run build first"]}
        hops = hops or self.hops
        q_vec = ngram_vector(query)
        q_phase = _query_phase(query)

        # adjacency: node_id -> list of (neighbor_id, relation, channel, weight)
        adj: dict[int, list[tuple[int, str, str, float]]] = {n["id"]: [] for n in nodes}
        for e in self.store.list_edges():
            adj.setdefault(e["src"], []).append((e["dst"], e["relation"], e["channel"], e["weight"]))
            adj.setdefault(e["dst"], []).append((e["src"], e["relation"], e["channel"], e["weight"]))

        # seed amplitudes: similarity of each node to the query
        node_by_id = {n["id"]: n for n in nodes}
        amp: dict[int, float] = {}
        for n in nodes:
            s = cosine(q_vec, n.get("vec") or {}) if q_vec else 0.0
            if s > 0:
                amp[n["id"]] = s

        # wave propagation (iterative diffusion with phase gating)
        path: dict[int, list[str]] = {nid: [] for nid in amp}
        frontier = dict(amp)  # node_id -> wave energy this round
        for _hop in range(hops):
            if not frontier:
                break
            nxt: dict[int, float] = {}
            for nid, energy in frontier.items():
                if energy < self.decay_floor:
                    continue
                for nbr, rel, ch, w in adj.get(nid, []):
                    decay = CHANNEL_DECAY.get(ch, 0.7)
                    contrib = energy * w * decay
                    if contrib < self.decay_floor:
                        continue
                    # phase gate: neighbor's phase close to query phase amplifies
                    phi_n = node_by_id[nbr].get("phase") or [0.0] * 6
                    d_phi = _phase_dist(q_phase, phi_n)
                    if d_phi < self.phase_gate:
                        contrib *= 1.0 + (1.0 - d_phi / self.phase_gate)  # up to 2x
                    nxt[nbr] = nxt.get(nbr, 0.0) + contrib
            for nid, contrib in nxt.items():
                amp[nid] = amp.get(nid, 0.0) + contrib
                if nid not in path:
                    path[nid] = []
                path[nid].append(f"{rel}@{ch}")
            frontier = nxt

        # rank + trim
        ranked = sorted(amp.items(), key=lambda kv: -kv[1])
        hits: list[dict[str, Any]] = []
        for nid, a in ranked:
            if a < self.amp_floor:
                break
            n = node_by_id[nid]
            hits.append(
                {
                    "node_id": nid,
                    "title": n["title"],
                    "amplitude": round(a, 4),
                    "path": path.get(nid, [])[-6:],
                    "x": n["x"],
                    "y": n["y"],
                    "similarity": round(amp.get(nid, 0.0), 4),
                }
            )
            if len(hits) >= k:
                break

        rid = self.store.record_resonance(query, hits)
        return {"run_id": rid, "hits": hits, "query_phase": q_phase}


def _query_phase(query: str) -> list[float]:
    """Coarse 6-dim semantic phase for a query (same cues as the builder)."""
    cues = {
        0: ("时间", "日期", "年", "月", "周报", "日报"),
        1: ("规则", "标准", "流程", "必须", "禁止"),
        2: ("证据", "数据", "来源", "引用", "实验"),
        3: ("属性", "特征", "参数", "尺寸", "颜色"),
        4: ("关系", "依赖", "关联", "协作"),
        5: ("实体", "名称", "模型", "系统", "软件"),
    }
    phase = [0.0] * 6
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
