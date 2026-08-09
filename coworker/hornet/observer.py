"""HORNET 2D — observer (涌现进化).

The meta-cognition layer: watches resonance patterns and grows new structure.

  1. hypernode (模式压缩): cells that co-resonate frequently become a super-node
     summary (title merged).
  2. attractor  (异常放大): high-similarity pairs whose contents diverge sharply
     (near-duplicate titles, very different bodies) are flagged as strange
     attractors — potential contradictions / innovation seeds.
  3. gap        (自我增殖): isolated cells (degree 0) or under-connected tiles
     are reported as knowledge gaps to fill.
  4. fission    (蜂胞分裂): high-load cells split into Z+/Z- child cells.
  5. cavity     (立体空洞): cells never resonated → topological cavities.
  6. conflict   (分层冲突): Z+ projection vs Z- traceback phase opposition.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .store import HornetStore, ngram_vector, similarity
from .builder import _phase_from_content_3d, _opposite_hint


class HornetObserver:
    def __init__(
        self, store: HornetStore, *, co_occurrence: int = 2, fission_threshold: float = 0.25
    ) -> None:
        self.store = store
        self.co_occurrence = co_occurrence
        self.fission_threshold = fission_threshold

    def _known_titles(self) -> list[str]:
        return [n["title"] for n in self.store.list_nodes()]

    def evolve(self, *, limit: int = 20) -> dict[str, Any]:
        emerged: list[dict[str, Any]] = []
        counts = {"hypernode": 0, "attractor": 0, "gap": 0, "fission": 0, "cavity": 0, "conflict": 0}

        # -- cell fission (spec §三维蜂胞分裂自扩展) -------------------------
        # A cell that resonates far more than its peers has a high load_factor —
        # it splits into Z+ projection / Z- traceback child cells, inheriting
        # the core semantics (self-similar growth, no physical void).
        stats = self.store.node_hit_stats(200)
        nodes = {n["id"]: n for n in self.store.list_nodes()}
        if stats:
            known_titles = [n["title"] for n in nodes.values()]
            hot = sorted(stats.items(), key=lambda kv: -kv[1]["load_factor"])
            fissioned: set[int] = set()
            for nid, st in hot[:6]:
                if st["load_factor"] < self.fission_threshold or nid in fissioned:
                    continue
                n = nodes.get(nid)
                if not n or not (n["content"] or "").strip():
                    continue
                # skip if this mother cell already fissioned (same title child exists)
                title = n["title"]
                if any(e.startswith(f"{title} · ") for e in known_titles):
                    continue
                z_child = "推演延伸" if n["z"] <= 0 else "溯源锚点"  # grow opposite zone
                child_title = f"{title[:40]} · {z_child}"
                child_content = _derive_child(n["content"], z_child)
                cid = self.store.add_node(
                    child_title, child_content[:2000], kb_item_id=n["kb_item_id"],
                    vec=ngram_vector(f"{child_title} {child_content}"),
                    phase=_phase_from_content_3d(f"{child_title} {child_content}"),
                    x=n["x"], y=n["y"], z=1 if z_child == "推演延伸" else -1,
                )
                self.store.add_edge(nid, cid, "similar", weight=0.8, channel="G3")
                fissioned.add(nid)
                emerged.append(
                    {"kind": "fission", "title": child_title,
                     "detail": {"mother": nid, "child": cid, "load_factor": st["load_factor"]}}
                )
                counts["fission"] += 1
                if len(emerged) >= limit:
                    break

        # -- hypernodes from co-resonance -------------------------------
        pairs: dict[tuple[int, int], int] = {}
        for rec in self.store.recent_resonance(100):
            hit_ids = [h.get("node_id") for h in rec.get("hits", []) if h.get("node_id")]
            for i in range(len(hit_ids)):
                for j in range(i + 1, len(hit_ids)):
                    key = tuple(sorted((hit_ids[i], hit_ids[j])))
                    pairs[key] = pairs.get(key, 0) + 1
        for (a, b), cnt in sorted(pairs.items(), key=lambda kv: -kv[1]):
            if cnt < self.co_occurrence:
                break
            na, nb = nodes.get(a), nodes.get(b)
            if not na or not nb:
                continue
            title = f"{na['title'][:24]} ⊕ {nb['title'][:24]}"
            if any(em.get("title") == title for em in emerged):
                continue
            emerged.append(
                {
                    "kind": "hypernode",
                    "title": title,
                    "detail": {
                        "members": [a, b],
                        "co_occurrences": cnt,
                        "summary": _merge_summary(na["content"], nb["content"]),
                    },
                }
            )
            counts["hypernode"] += 1
            if len(emerged) >= limit:
                break

        # -- attractors: similar titles, divergent bodies ----------------
        edges = self.store.list_edges()
        seen: set[tuple[int, int]] = set()
        for e in edges:
            if e["relation"] != "similar":
                continue
            key = tuple(sorted((e["src"], e["dst"])))
            if key in seen:
                continue
            seen.add(key)
            na, nb = nodes.get(e["src"]), nodes.get(e["dst"])
            if not na or not nb:
                continue
            ta, tb = (na["content"] or ""), (nb["content"] or "")
            if not ta or not tb:
                continue
            sim = similarity(ta[:600], tb[:600])
            # same-ish title, low body similarity → suspicious divergence
            if _titles_overlap(na["title"], nb["title"]) and sim < 0.15 and len(ta) > 80 and len(tb) > 80:
                emerged.append(
                    {
                        "kind": "attractor",
                        "title": f"奇异吸引子: {na['title'][:28]} ↔ {nb['title'][:28]}",
                        "detail": {"nodes": [na["id"], nb["id"]], "body_similarity": round(sim, 4)},
                    }
                )
                counts["attractor"] += 1
                if len(emerged) >= limit:
                    break

        # -- gaps: topological cavities (spec §立体空洞挖掘) -----------------
        # Not just isolated cells: cells that NEVER resonate (zero amplitude in
        # the resonance history) form knowledge cavities — the system flags them
        # and suggests a probe wave to actively fill the void.
        degree: dict[int, int] = {}
        for e in edges:
            degree[e["src"]] = degree.get(e["src"], 0) + 1
            degree[e["dst"]] = degree.get(e["dst"], 0) + 1
        hit_ids = set(stats.keys())
        for nid, n in nodes.items():
            if degree.get(nid, 0) == 0 and (n["content"] or ""):
                emerged.append(
                    {
                        "kind": "gap",
                        "title": f"知识空洞: {n['title'][:40]}",
                        "detail": {"node_id": nid, "hint": "该节点没有语义连接,建议补充关联或合并"},
                    }
                )
                counts["gap"] += 1
                if len(emerged) >= limit:
                    break
        for nid, n in nodes.items():
            if len(emerged) >= limit:
                break
            if nid in hit_ids or degree.get(nid, 0) == 0 or not (n["content"] or ""):
                continue
            emerged.append(
                {
                    "kind": "cavity",
                    "title": f"拓扑腔体: {n['title'][:40]}",
                    "detail": {
                        "node_id": nid,
                        "hint": "该蜂房从未被任何探针波共振命中,建议定向探测补全",
                        "probe": n["title"][:60],
                    },
                }
            )
            counts["cavity"] += 1

        # -- layered conflict (spec §分层冲突涌现) ---------------------------
        # Z+ projection cells whose content contradicts Z- traceback cells:
        # phase opposition across zones marks a high-grade strange attractor
        # that deserves human attention first.
        zplus = [n for n in nodes.values() if n.get("z", 0) > 0]
        zminus = [n for n in nodes.values() if n.get("z", 0) < 0]
        for zp in zplus:
            if len(emerged) >= limit:
                break
            for zm in zminus:
                if _opposite_hint(zp["title"], zm["title"]):
                    emerged.append(
                        {
                            "kind": "conflict",
                            "title": f"分层冲突: {zp['title'][:24]} ↔ {zm['title'][:24]}",
                            "detail": {
                                "z_plus": zp["id"],
                                "z_minus": zm["id"],
                                "hint": "推演层与溯源层相位对冲,优先人工介入",
                            },
                        }
                    )
                    counts["conflict"] += 1
                    break

        saved = 0
        for em in emerged:
            eid, is_new = self.store.add_emergent(em["kind"], em["title"], em["detail"])
            em["id"] = eid
            em["is_new"] = is_new
            if is_new:
                saved += 1
        return {"emerged": saved, "counts": counts, "items": emerged}


def _derive_child(content: str, kind: str) -> str:
    """Self-similar growth: a child cell inherits the core text plus a zone
    cue. Z+ projection appends forward-looking prompts; Z- traceback keeps the
    factual core for anchoring."""
    core = (content or "").strip()[:1200]
    if kind == "推演延伸":
        return core + "\n[推演延伸] 该知识的未来发展方向与潜在推演:需要补充预测与规划。"
    return core + "\n[溯源锚点] 该知识的起源与历史证据锚定:需要补充来源与背景。"


def _titles_overlap(a: str, b: str) -> bool:
    """Crude title kinship: share a >=4-char token or one contains the other."""
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    sa = {t for t in a.split() if len(t) >= 4}
    sb = {t for t in b.split() if len(t) >= 4}
    return bool(sa & sb)


def _merge_summary(ca: str, cb: str) -> str:
    a = (ca or "").strip()
    b = (cb or "").strip()
    return (a[:160] + " … | … " + b[:160]).replace("\n", " ")
