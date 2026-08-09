"""HORNET 2D — observer (涌现进化).

The meta-cognition layer: watches resonance patterns and grows new structure.

  1. hypernode (模式压缩): cells that co-resonate frequently become a super-node
     summary (title merged).
  2. attractor  (异常放大): high-similarity pairs whose contents diverge sharply
     (near-duplicate titles, very different bodies) are flagged as strange
     attractors — potential contradictions / innovation seeds.
  3. gap        (自我增殖): isolated cells (degree 0) or under-connected tiles
     are reported as knowledge gaps to fill.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .store import HornetStore, similarity


class HornetObserver:
    def __init__(self, store: HornetStore, *, co_occurrence: int = 2) -> None:
        self.store = store
        self.co_occurrence = co_occurrence

    def evolve(self, *, limit: int = 20) -> dict[str, Any]:
        emerged: list[dict[str, Any]] = []
        counts = {"hypernode": 0, "attractor": 0, "gap": 0}

        # -- hypernodes from co-resonance -------------------------------
        nodes = {n["id"]: n for n in self.store.list_nodes()}
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

        # -- gaps: isolated cells -----------------------------------------
        degree: dict[int, int] = {}
        for e in edges:
            degree[e["src"]] = degree.get(e["src"], 0) + 1
            degree[e["dst"]] = degree.get(e["dst"], 0) + 1
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

        saved = 0
        for em in emerged:
            self.store.add_emergent(em["kind"], em["title"], em["detail"])
            saved += 1
        return {"emerged": saved, "counts": counts, "items": emerged}


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
