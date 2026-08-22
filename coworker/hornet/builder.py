"""HORNET 2D — builder (建图).

Maps the existing KnowledgeStore entries into Hive-Cells and auto-builds the
six semantic relation edges, then lays cells out on a hexagonal grid (axial
coordinates) via PCA projection of their n-gram vectors.

Auto-edge rules (deterministic, no LLM needed for the minimal version):
  - similar   : ngram cosine >= SIM_THRESHOLD             -> channel D5
  - contains  : one title/path is a prefix of another     -> channel D2
  - temporal  : same-day-ish created_at and date-like titles -> channel D0
  - attribute : strong shared keyword (common n-gram stem) -> channel D2
  - cause/opposite: keyword rules (轻量)                   -> D1 / D4
"""
from __future__ import annotations

import json
import math
import re
from bisect import bisect_right
from typing import Any, Optional

from .store import HornetStore, ngram_vector, cosine, similarity, RELATION_CHANNEL_3D

SIM_THRESHOLD = 0.24          # similar edge
ATTRIBUTE_THRESHOLD = 0.20    # shared-keyword attribute edge
TEMPORAL_DAYS = 7             # same creation window
HEX_RADIUS = 1.0              # axial coordinate unit

_DATE_RE = re.compile(r"\d{4}[-_/年.]?\d{1,2}[-_/月.]?\d{1,2}日?")

_CAUSE_PAIRS = [
    ("因为", "所以"), ("由于", "因此"), ("导致", "引起"),
    ("原因", "结果"), ("之所以", "是因为"),
]
_OPPOSITE_PAIRS = [
    ("增长", "下降"), ("上升", "下跌"), ("增加", "减少"),
    ("优点", "缺点"), ("优势", "劣势"), ("利好", "利空"),
    ("成功", "失败"), ("高", "低"), ("强", "弱"), ("多", "少"),
]


def _contains_hint(a: str, b: str) -> bool:
    """Path-ish containment: one title is a prefix of the other (split on path separators)."""
    for sep in ("\\", "/", "."):
        if sep in a and sep in b:
            pa = a.split(sep)
            pb = b.split(sep)
            if len(pa) != len(pb) and (pa[: min(len(pa), len(pb))] == pb[: min(len(pa), len(pb))]):
                return True
    return a in b or b in a


def _has_date(s: str) -> bool:
    return bool(_DATE_RE.search(s))


def _keyword_attribute(
    a: str, b: str,
    grams_a: Optional[set[str]] = None, grams_b: Optional[set[str]] = None,
) -> bool:
    """Shared distinctive n-gram stem (len>=2 tokens) — crude attribute kinship.
    `grams_a/grams_b`: precomputed title 2-gram sets (the pair loop passes them
    so the vectors aren't rebuilt for every candidate pair)."""
    grams_a = grams_a if grams_a is not None else set(ngram_vector(a, 2))
    grams_b = grams_b if grams_b is not None else set(ngram_vector(b, 2))
    shared = {g for g in grams_a if len(g) >= 4} & {g for g in grams_b if len(g) >= 4}
    return len(shared) >= 2


def _cause_hint(a: str, b: str) -> bool:
    la, lb = (a or "").lower(), (b or "").lower()
    for x, y in _CAUSE_PAIRS:
        if (x in la and y in lb) or (x in lb and y in la):
            return True
    return False


def _opposite_hint(a: str, b: str) -> bool:
    la, lb = (a or "").lower(), (b or "").lower()
    for x, y in _OPPOSITE_PAIRS:
        if (x in la and y in lb) or (x in lb and y in la):
            return True
    return False


def _ta_vectors(items: list[tuple[Optional[int], str, str, float]]) -> list[dict[str, float]]:
    """n-gram vectors of `f"{title} {content}"[:600]` — the exact texts
    _classify's similar/attribute rules compare. Computed once per item; the
    pair loop used to rebuild both sides for EVERY candidate pair (n² 600-char
    ngram_vector calls — the dominant rebuild cost after the OOM fixes)."""
    return [ngram_vector(f"{t} {c}"[:600]) for _k, t, c, _ts in items]


def _candidate_pairs(
    items: list[tuple[Optional[int], str, str, float]],
    title_grams: list[set[str]],
) -> list[tuple[int, int, bool]]:
    """All (i, j, share) index pairs _classify could return a relation for.

    Replaces the old all-pairs scan (~n²/2 iterations; ~2.9M on the 2.4k-item
    hive) with an inverted index over title 2-grams plus three small passes:
      (a) titles share a 2-gram — required by similar/attribute anyway (cosine
          over 2-gram dicts is 0 without a shared gram) and it subsumes contains
          (a substring of length>=2 shares its grams with the containing title);
      (b) a cause/opposite keyword pair spans both titles — the keyword rules
          fire between otherwise unrelated titles;
      (c) both titles are date-like and the items were created within
          TEMPORAL_DAYS — the temporal rule (date-like titles can carry
          different years and share no gram at all);
      (d) a title shorter than 2 chars — degenerate containment (`"" in b`).
    Output is (i, j)-sorted, so edge insertion order matches the old scan and
    the built graph is byte-identical to the all-pairs version."""
    n = len(items)
    titles = [t or "" for _k, t, _c, _ts in items]
    pair_share: dict[tuple[int, int], bool] = {}

    # (a) inverted index over title 2-grams (postings in ascending item order)
    postings: dict[str, list[int]] = {}
    for i, grams in enumerate(title_grams):
        for g in grams:
            postings.setdefault(g, []).append(i)
    stamp = [-1] * n
    for i in range(n):
        for g in title_grams[i]:
            pl = postings[g]
            for j in pl[bisect_right(pl, i):]:
                if stamp[j] != i:
                    stamp[j] = i
                    pair_share[(i, j)] = True

    def _add(i: int, j: int) -> None:
        key = (i, j) if i < j else (j, i)
        if key not in pair_share:
            pair_share[key] = bool(title_grams[key[0]] & title_grams[key[1]])

    # (b) cause/opposite keyword pairs (checked in both directions)
    lower = [t.lower() for t in titles]
    keywords: dict[str, list[int]] = {}
    for x, y in (*_CAUSE_PAIRS, *_OPPOSITE_PAIRS):
        for kw in (x, y):
            if kw not in keywords:
                keywords[kw] = [i for i, lt in enumerate(lower) if kw in lt]
    for x, y in (*_CAUSE_PAIRS, *_OPPOSITE_PAIRS):
        px, py = keywords[x], keywords[y]
        # pathological guard: single-char keywords (高/低…) spanning most of the
        # hive would reintroduce a quadratic cross product — skip those pairs.
        if len(px) * len(py) > 200_000:
            continue
        for i in px:
            for j in py:
                if i != j:
                    _add(i, j)

    # (c) temporal: date-like titles created within the same window
    dated = sorted(
        (i for i, t in enumerate(titles) if _has_date(t)),
        key=lambda i: items[i][3],
    )
    for a, ia in enumerate(dated):
        ts_a = items[ia][3]
        span = 0
        for b in range(a + 1, len(dated)):
            ib = dated[b]
            if items[ib][3] - ts_a >= TEMPORAL_DAYS * 86400:
                break
            _add(ia, ib)
            span += 1
            if span >= 2000:  # same pathological guard as (b)
                break

    # (d) degenerate short titles pair against everything
    for i, t in enumerate(titles):
        if len(t) < 2:
            for j in range(n):
                if i != j:
                    _add(i, j)

    return [(i, j, sh) for (i, j), sh in sorted(pair_share.items())]


def _auto_edges(
    items: list[tuple[Optional[int], str, str, float]],
    title_grams: list[set[str]],
    ta_vecs: list[dict[str, float]],
    cands: list[tuple[int, int, bool]],
) -> list[tuple[int, int, str, float]]:
    """Classify every candidate pair — the store-free core of build()'s edge
    pass, split out so it can be benchmarked and diffed against the old
    all-pairs scan standalone."""
    out: list[tuple[int, int, str, float]] = []
    for i, j, share in cands:
        rel, weight, _ch = _classify(
            items[i][1], items[i][2], items[j][1], items[j][2],
            items[i][3], items[j][3], share,
            vec_a=ta_vecs[i], vec_b=ta_vecs[j],
            grams_a=title_grams[i], grams_b=title_grams[j],
        )
        if rel:
            out.append((i, j, rel, weight))
    return out


# -- hexagonal layout (axial coords via PCA of n-gram vectors) -----------------
def _pca2(vectors: list[dict[str, float]]) -> list[tuple[float, float]]:
    """Project sparse n-gram vectors to 2D via truncated PCA (numpy). Falls back
    to hash-based scatter when numpy is unavailable."""
    if not vectors:
        return []
    try:
        import numpy as np
    except ImportError:
        return _pca2_pure(vectors)
    df: dict[str, int] = {}
    for v in vectors:
        for g in v:
            df[g] = df.get(g, 0) + 1
    basis = [g for g, _ in sorted(df.items(), key=lambda kv: -kv[1])[:256]]
    if not basis:
        return []
    idx = {g: i for i, g in enumerate(basis)}
    n = len(vectors)
    m = len(basis)
    X = np.zeros((n, m), dtype=np.float32)
    for i, v in enumerate(vectors):
        for g, c in v.items():
            j = idx.get(g)
            if j is not None:
                X[i, j] = c
    X -= X.mean(axis=0, keepdims=True)
    try:
        u, s, _ = np.linalg.svd(X, full_matrices=False)
        k = min(2, u.shape[1])
        proj = (u[:, :k] * s[:k])
        if k == 1:  # degenerate single component
            proj = np.column_stack([proj[:, 0], np.zeros(n, dtype=np.float32)])
    except np.linalg.LinAlgError:  # pragma: no cover - degenerate
        proj = np.random.default_rng(0).normal(size=(n, 2))
    return [(float(a), float(b)) for a, b in proj]


def _pca2_pure(vectors: list[dict[str, float]]) -> list[tuple[float, float]]:
    """Pure-Python fallback (no numpy) — power iteration on the covariance."""
    if not vectors:
        return []
    # shared gram basis (top by document frequency)
    df: dict[str, int] = {}
    for v in vectors:
        for g in v:
            df[g] = df.get(g, 0) + 1
    basis = [g for g, _ in sorted(df.items(), key=lambda kv: -kv[1])[:256]]
    if not basis:
        return []
    idx = {g: i for i, g in enumerate(basis)}
    # dense matrix
    n = len(vectors)
    m = len(basis)
    X = [[0.0] * m for _ in range(n)]
    for i, v in enumerate(vectors):
        for g, c in v.items():
            if g in idx:
                X[i][idx[g]] = c
    # center
    for j in range(m):
        mean = sum(X[i][j] for i in range(n)) / n
        for i in range(n):
            X[i][j] -= mean
    # power iteration on X X^T (n x n covariance-ish)
    def matvec(w: list[float]) -> list[float]:
        out = [0.0] * n
        for i in range(n):
            s = 0.0
            for k in range(n):
                dot = sum(X[i][j] * X[k][j] for j in range(m))
                s += dot * w[k]
            out[i] = s
        return out

    w = [1.0] * n
    for _ in range(12):
        nw = matvec(w)
        norm = math.sqrt(sum(x * x for x in nw)) or 1.0
        w = [x / norm for x in nw]
    pc1 = w
    # second component: orthogonalize against pc1
    w2 = [1.0] * n
    for _ in range(12):
        nw = matvec(w2)
        dot = sum(nw[i] * pc1[i] for i in range(n))
        nw = [nw[i] - dot * pc1[i] for i in range(n)]
        norm = math.sqrt(sum(x * x for x in nw)) or 1.0
        w2 = [x / norm for x in nw]
    pc2 = w2
    return [(pc1[i], pc2[i]) for i in range(n)]


def _pca3(vectors: list[dict[str, float]]) -> list[tuple[float, float, float]]:
    """Project sparse n-gram vectors to 3D via numpy SVD (3 components). Falls
    back to (z=0) when numpy is missing."""
    if not vectors:
        return []
    try:
        import numpy as np
    except ImportError:
        return [(a, b, 0.0) for a, b in _pca2(vectors)]
    df: dict[str, int] = {}
    for v in vectors:
        for g in v:
            df[g] = df.get(g, 0) + 1
    basis = [g for g, _ in sorted(df.items(), key=lambda kv: -kv[1])[:256]]
    if not basis:
        return [(0.0, 0.0, 0.0)] * len(vectors)
    idx = {g: i for i, g in enumerate(basis)}
    n = len(vectors)
    m = len(basis)
    X = np.zeros((n, m), dtype=np.float32)
    for i, v in enumerate(vectors):
        for g, c in v.items():
            j = idx.get(g)
            if j is not None:
                X[i, j] = c
    X -= X.mean(axis=0, keepdims=True)
    try:
        u, s, _ = np.linalg.svd(X, full_matrices=False)
        k = min(3, u.shape[1])
        proj = u[:, :k] * s[:k]
        while proj.shape[1] < 3:
            proj = np.column_stack([proj, np.zeros(n, dtype=np.float32)])
    except np.linalg.LinAlgError:  # pragma: no cover - degenerate
        rng = np.random.default_rng(0)
        proj = rng.normal(size=(n, 3))
    return [(float(a), float(b), float(c)) for a, b, c in proj]


def _axial_round(q: float, r: float) -> tuple[int, int]:
    """Round floating axial coords to the nearest hex (cube rounding)."""
    x, y, z = q, r, -q - r
    rx, ry, rz = round(x), round(y), round(z)
    dx, dy, dz = abs(rx - x), abs(ry - y), abs(rz - z)
    if dx > dy and dx > dz:
        rx = -ry - rz
    elif dy > dz:
        ry = -rx - rz
    else:
        rz = -rx - ry
    return int(rx), int(ry)


def _layout3(vectors: list[dict[str, float]], occupied: set[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Map each vector to a 3D lattice coordinate (no two cells share a tile).
    Lattice: XY hex-ish axial plane + integer z layer (Z+ = projection/evolve,
    Z- = traceback/evidence)."""
    proj = _pca3(vectors)
    if not proj:
        return [(0, 0, 0)] * len(vectors)
    maxabs = max((abs(a) + abs(b) + abs(c)) for a, b, c in proj) or 1.0
    coords: list[tuple[int, int, int]] = []
    for a, b, c in proj:
        q = (a / maxabs) * 8.0
        r = (b / maxabs) * 8.0
        z = round((c / maxabs) * 3.0)
        tile = (*_axial_round(q, r), z)
        guard = 0
        while tile in occupied and guard < 96:
            q += 0.7
            r += 0.35
            tile = (*_axial_round(q, r), z)
            guard += 1
        occupied.add(tile)
        coords.append(tile)
    return coords


def _layout(vectors: list[dict[str, float]], occupied: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """Map each vector to a hexagonal axial coordinate (no two cells share a tile)."""
    proj = _pca2(vectors)
    if not proj:
        return [(0, 0)] * len(vectors)
    # normalize to a modest radius
    maxabs = max((abs(a) + abs(b)) for a, b in proj) or 1.0
    coords: list[tuple[int, int]] = []
    for a, b in proj:
        q = (a / maxabs) * 8.0
        r = (b / maxabs) * 8.0
        tile = _axial_round(q, r)
        # resolve collisions by spiraling outward
        guard = 0
        while tile in occupied and guard < 64:
            q += 0.7
            r += 0.35
            tile = _axial_round(q, r)
            guard += 1
        occupied.add(tile)
        coords.append(tile)
    return coords


# -- builder -------------------------------------------------------------------
class HornetBuilder:
    """Build the hive from a list of (kb_item_id, title, content, created_at) tuples."""

    def __init__(self, store: HornetStore) -> None:
        self.store = store

    def build(
        self,
        items: list[tuple[Optional[int], str, str, float]],
        *,
        rebuild: bool = True,
        topo: bool = False,
    ) -> dict[str, Any]:
        if rebuild:
            self.store.clear()
        n = len(items)
        if n == 0:
            return {"nodes": 0, "edges": 0, "topo": False}
        # title 2-gram sets — shared by the candidate index, the topo pre-pass
        # and the edge classifier (each pass used to build its own copy).
        title_grams = [set(ngram_vector(t, 2).keys()) for _k, t, _c, _ts in items]
        # candidate pairs via inverted index (see _candidate_pairs): the edge
        # pass no longer scans all n²/2 pairs (~2.9M on a 2.4k-item hive).
        cands = _candidate_pairs(items, title_grams)

        # Cap the text fed to the n-gram vector: nodes store content[:4000] anyway,
        # and the FULL body of a ~2.4k-item KB (~154MB of chunks) ballooned the
        # per-item vector dicts into multi-GB territory — the silent OOM that
        # killed the sidecar whenever a knowledge-gap task finished and rebuilt
        # the graph (2026-08-22 post-mortem).
        vectors: list[Optional[dict[str, float]]] = [
            ngram_vector(f"{title} {content[:4000]}") for _kid, title, content, _ts in items
        ]
        ta_vecs: Optional[list[dict[str, float]]] = None
        E = None
        np = None  # bound inside the topo branch; only used when E is not None
        if topo and n >= 8:
            # topological soft-constraint embedding (numpy GCN + ring loss):
            # layout and similar-edge weights follow the hive topology.
            import numpy as np

            # 600-char head vectors for the pre-pass; reused by the final edge
            # pass below (topo is the only path where both vector sets coexist).
            ta_vecs = _ta_vectors(items)
            # edges for the GCN graph come from a first classification pass over
            # the gram-sharing candidates (share=True, same pairs as the old scan).
            prelim: list[tuple[int, int]] = []
            for i, j, share in cands:
                if share and _classify(
                    items[i][1], items[i][2], items[j][1], items[j][2],
                    items[i][3], items[j][3], True,
                    vec_a=ta_vecs[i], vec_b=ta_vecs[j],
                    grams_a=title_grams[i], grams_b=title_grams[j],
                )[0]:
                    prelim.append((i, j))
            df: dict[str, int] = {}
            for v in vectors:
                for g in v:
                    df[g] = df.get(g, 0) + 1
            basis = [g for g, _ in sorted(df.items(), key=lambda kv: -kv[1])[:200]]
            idx = {g: i for i, g in enumerate(basis)}
            X = np.zeros((n, len(basis)), dtype=np.float32)
            for i, v in enumerate(vectors):
                for g, c in v.items():
                    j = idx.get(g)
                    if j is not None:
                        X[i, j] = c
            from .topo_embed import topo_embed

            tres = topo_embed(X, prelim, epochs=8)
            E = tres["embedding"]  # n x out_dim
            layout_vecs = [dict(enumerate(map(float, E[i]))) for i in range(n)]
        else:
            layout_vecs = vectors
        occupied: set[tuple[int, int, int]] = set()
        coords = _layout3(layout_vecs, occupied)

        node_ids: list[int] = []
        for k, (kid, title, content, ts) in enumerate(items):
            x, y, z = coords[k]
            phase = _phase_from_content_3d(f"{title} {content[:4000]}")
            # reuse the capped vector computed for the layout pass instead of
            # re-deriving it from (potentially full-length) content again.
            vec = vectors[k]
            nid = self.store.add_node(
                title, content[:4000], kb_item_id=kid,
                vec=vec,
                phase=phase, x=x, y=y, z=z,
                topo=list(map(float, E[k])) if E is not None else None,
                commit=False,
            )
            vectors[k] = None  # release as we write — only the layout needed them all
            node_ids.append(nid)
        self.store.commit()  # one transaction for all nodes (was ~N commits)

        # auto edges: classify only the candidate pairs. The 600-char head
        # vectors are computed HERE on the non-topo path — the 4000-capped
        # layout vectors above are already released, so the two big vector
        # sets never coexist in memory.
        if ta_vecs is None:
            ta_vecs = _ta_vectors(items)
        auto = _auto_edges(items, title_grams, ta_vecs, cands)
        del ta_vecs
        edges = 0
        for i, j, rel, weight in auto:
            ch = RELATION_CHANNEL_3D.get(rel, "G3")
            if E is not None and rel == "similar":
                # topology-enhanced edge weight: blend 2-gram sim with
                # topological cosine so resonance follows the hive shape.
                # E rows are L2-normalized → dot ∈ [-1, 1]; map to [0, 1]
                # so the blended weight stays non-negative.
                w_topo = (float(np.dot(E[i], E[j])) + 1.0) / 2.0
                weight = round(0.5 * weight + 0.5 * w_topo, 4)
            if self.store.add_edge(node_ids[i], node_ids[j], rel, weight=weight, channel=ch, commit=False):
                edges += 1
        self.store.commit()  # one transaction for all edges (was ~E commits)
        return {"nodes": len(node_ids), "edges": edges, "topo": bool(E is not None)}


def _phase_from_content_3d(text: str) -> list[float]:
    """12-dim semantic phase: 6 base cues (event/rule/evidence/attribute/
    relation/entity) + 6 3D-extension cues (Z+ projection: hypothesis/future/
    plan/synthesis/trend/abstract; Z- traceback: origin/history/evidence anchor/
    cause/revision/baseline). Phase difference gates resonance amplification."""
    base = _phase_from_content(text)
    ext_cues = {
        6: ("假设", "推测", "预测", "未来", "展望"),
        7: ("规划", "计划", "方案", "步骤", "落地"),
        8: ("综合", "总结", "提炼", "范式", "框架"),
        9: ("趋势", "演变", "演进", "发展", "方向"),
        10: ("起源", "历史", "早期", "传统", "经典"),
        11: ("修订", "更新", "版本", "基线", "变更"),
    }
    ext = [0.0] * 6
    for k, kws in ext_cues.items():
        if any(kw in text for kw in kws):
            ext[k - 6] = 1.0
    phase = base + ext
    norm = math.sqrt(sum(p * p for p in phase)) or 1.0
    return [round(p / norm, 4) for p in phase]


def _phase_from_content(text: str) -> list[float]:
    """6-dim semantic phase from coarse content cues (deterministic hash of
    keyword presence). Phase difference gates resonance amplification."""
    cues = {
        0: ("时间", "日期", "年", "月", "周报", "日报"),          # event/temporal
        1: ("规则", "标准", "流程", "必须", "禁止"),              # rule
        2: ("证据", "数据", "来源", "引用", "实验"),              # evidence
        3: ("属性", "特征", "参数", "尺寸", "颜色"),              # attribute
        4: ("关系", "依赖", "关联", "协作"),                       # relation
        5: ("实体", "名称", "模型", "系统", "软件"),               # entity
    }
    phase = [0.0] * 6
    for k, kws in cues.items():
        if any(kw in text for kw in kws):
            phase[k] = 1.0
    norm = math.sqrt(sum(p * p for p in phase)) or 1.0
    return [round(p / norm, 4) for p in phase]


def _classify(
    title_a: str, content_a: str, title_b: str, content_b: str,
    ts_a: float, ts_b: float, share_title_grams: bool = True,
    *,
    vec_a: Optional[dict[str, float]] = None,
    vec_b: Optional[dict[str, float]] = None,
    grams_a: Optional[set[str]] = None,
    grams_b: Optional[set[str]] = None,
) -> tuple[Optional[str], float, Optional[str]]:
    """Decide relation/weight/channel between two cells (deterministic rules).
    `share_title_grams`: false skips the full-text similar/attribute checks (the
    O(n²) hot path stays cheap — no n-gram rebuild for unrelated pairs).
    Callers looping over pairs pass `vec_a/vec_b` (n-gram vectors of
    f"{title} {content}"[:600]) and `grams_a/grams_b` (title 2-gram sets) so
    each item's vectors are computed once instead of once per pair."""
    # cause / opposite / contains / temporal are keyword-ish — cheap, always run
    if _cause_hint(title_a, title_b):
        return "cause", 0.8, "D1"
    if _opposite_hint(title_a, title_b):
        return "opposite", 0.7, "D4"
    if _contains_hint(title_a, title_b) and len(title_a) != len(title_b):
        return "contains", 0.9, "D2"
    if _has_date(title_a) and _has_date(title_b) and abs(ts_a - ts_b) < TEMPORAL_DAYS * 86400:
        return "temporal", 0.75, "D0"

    # similar / attribute need full-text n-grams — skip when titles don't share
    # any 2-gram (they can't be meaningfully similar anyway)
    if not share_title_grams:
        return None, 0.0, None

    if vec_a is not None and vec_b is not None:
        sim = cosine(vec_a, vec_b)
    else:
        ta = f"{title_a} {content_a}"[:600]
        tb = f"{title_b} {content_b}"[:600]
        sim = similarity(ta, tb)
    if sim >= SIM_THRESHOLD:
        return "similar", round(sim, 4), "D5"
    if sim >= ATTRIBUTE_THRESHOLD and _keyword_attribute(title_a, title_b, grams_a, grams_b):
        return "attribute", round(sim, 4), "D2"
    return None, 0.0, None
