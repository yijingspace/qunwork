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
from typing import Any, Optional

from .store import HornetStore, ngram_vector, cosine, similarity

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


def _keyword_attribute(a: str, b: str) -> bool:
    """Shared distinctive n-gram stem (len>=2 tokens) — crude attribute kinship."""
    grams_a = {g for g in ngram_vector(a, 2) if len(g) >= 4}
    grams_b = {g for g in ngram_vector(b, 2) if len(g) >= 4}
    shared = grams_a & grams_b
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
    ) -> dict[str, Any]:
        if rebuild:
            self.store.clear()
        vectors: list[dict[str, float]] = []
        for _kid, title, content, _ts in items:
            vectors.append(ngram_vector(f"{title} {content}"))
        occupied: set[tuple[int, int]] = set()
        coords = _layout(vectors, occupied)

        node_ids: list[int] = []
        for (kid, title, content, ts), (x, y) in zip(items, coords):
            phase = _phase_from_content(f"{title} {content}")
            nid = self.store.add_node(
                title, content[:4000], kb_item_id=kid,
                vec=ngram_vector(f"{title} {content}"),
                phase=phase, x=x, y=y,
            )
            node_ids.append(nid)

        # auto edges (title-gram prefilter keeps the O(n²) pass cheap: full-text
        # similarity only runs when two titles share a 2-gram)
        edges = 0
        n = len(items)
        title_grams = [set(ngram_vector(t, 2).keys()) for _k, t, _c, _ts in items]
        for i in range(n):
            ti, ci = items[i][1], items[i][2]
            gi = title_grams[i]
            for j in range(i + 1, n):
                tj, cj = items[j][1], items[j][2]
                share = bool(gi & title_grams[j])
                rel, weight, channel = _classify(ti, ci, tj, cj, items[i][3], items[j][3], share)
                if rel:
                    if self.store.add_edge(node_ids[i], node_ids[j], rel, weight=weight, channel=channel):
                        edges += 1
        return {"nodes": len(node_ids), "edges": edges}


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
) -> tuple[Optional[str], float, Optional[str]]:
    """Decide relation/weight/channel between two cells (deterministic rules).
    `share_title_grams`: false skips the full-text similar/attribute checks (the
    O(n²) hot path stays cheap — no n-gram rebuild for unrelated pairs)."""
    ta = f"{title_a} {content_a}"[:600]
    tb = f"{title_b} {content_b}"[:600]

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

    sim = similarity(ta, tb)
    if sim >= SIM_THRESHOLD:
        return "similar", round(sim, 4), "D5"
    if sim >= ATTRIBUTE_THRESHOLD and _keyword_attribute(title_a, title_b):
        return "attribute", round(sim, 4), "D2"
    return None, 0.0, None
