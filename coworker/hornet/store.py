"""HORNET (蜂巢共振神经拓扑) 2D layer — store.

A knowledge layer that sits ON TOP of the existing KnowledgeStore (data stays in
knowledge.db; HORNET adds a hive of semantically-connected cells). Design follows
E:/QunWork/自研HorNet架构新一代知识库/HORNET(蜂巢共振神经拓扑)架构:

  - Hive-Cell (蜂房)  = a knowledge node with a 6-dim semantic phase vector.
  - Geometric layer  = 6 direction channels D0..D5 (pure transport: decay only).
  - Semantic layer   = 6 relation kinds dynamically mounted on channels.
  - Resonance        = graph wave propagation with phase-matched amplification.
  - Emergence        = observer AI turning resonance patterns into new structure.

This file is the SQLite store. Vectors are char n-gram dicts (same scheme as the
knowledge store) — fully local, Chinese-friendly, no external embedder.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

# 6 direction channels (geometric layer, transport only)
CHANNELS = ("D0", "D1", "D2", "D3", "D4", "D5")
# 3D upgrade: 12 geometric channels G0..G11 (Kelvin-cell abstraction from the
# HORNET spec) — XY plane (G0-G3), Z+ projection (G4-G7), Z- traceback (G8-G11).
CHANNELS_3D = (
    "G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10", "G11",
)
# Zone decay: Z+ (forward/projection) diffuses fast; Z- (traceback/evidence)
# converges hard; XY plane sits between. Per spec §三维 12 通道几何方位编码.
CHANNEL_DECAY_3D = {
    "G0": 0.72, "G1": 0.62, "G2": 0.78, "G3": 0.72,   # XY 平面
    "G4": 0.55, "G5": 0.55, "G6": 0.55, "G7": 0.55,   # Z+ 推演(衰减小,易扩散)
    "G8": 0.90, "G9": 0.90, "G10": 0.90, "G11": 0.90, # Z- 溯源(衰减大,收敛强)
}

# 6 semantic relation kinds (semantic layer)
RELATIONS = ("cause", "similar", "opposite", "contains", "temporal", "attribute")
# relation -> default channel (semantics are dynamically bindable; this is the seed)
RELATION_CHANNEL = {
    "cause": "D1",      # causal output (东南)
    "similar": "D5",    # entity association (东北)
    "opposite": "D4",   # causal traceback / contrast (西北)
    "contains": "D2",   # attribute expansion (西南)
    "temporal": "D0",   # temporal forward (东)
    "attribute": "D2",  # attribute expansion (西南)
}
# 3D variant: semantic edges bind to a 12-channel direction (semantics stay
# dynamically bindable; this is the 3D seed mapping).
RELATION_CHANNEL_3D = {
    "cause": "G1",      # causal output (XY 东南向)
    "similar": "G3",    # entity association (XY 东北向)
    "opposite": "G8",   # contrast sinks to Z- (溯源/证据层)
    "contains": "G2",   # attribute expansion (XY 西南向)
    "temporal": "G0",   # temporal forward (XY 东向)
    "attribute": "G6",  # attribute expands upward (Z+ 外扩)
}
EMERGENT_KINDS = ("hypernode", "attractor", "gap")


def ngram_vector(text: str, n: int = 2) -> dict[str, float]:
    """Char n-gram count vector (normalized) — decent Chinese similarity.
    Default n=2: Chinese 2-char words (周报/目标/达成) become grams; n=3 drops
    them entirely and short queries stop matching."""
    text = re.sub(r"\s+", "", (text or "").lower())
    if len(text) < n:
        return {text: 1.0} if text else {}
    counts: dict[str, int] = {}
    for i in range(len(text) - n + 1):
        gram = text[i : i + n]
        counts[gram] = counts.get(gram, 0) + 1
    norm = math.sqrt(sum(c * c for c in counts.values())) or 1.0
    return {g: c / norm for g, c in counts.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a) & set(b)
        if not keys:
            return 0.0
        return sum(a[k] * b[k] for k in keys)
    return 0.0


def _similarity(text_a: str, text_b: str) -> float:
    """Query-coverage style similarity on n-grams (doc side not length-normalized)."""
    q = ngram_vector(text_a)
    d = ngram_vector(text_b)
    return cosine(q, d)


def coverage_similarity(query: str, doc_vec: dict[str, float], n: int = 2) -> float:
    """Query raw n-gram counts x doc normalized coefficients — NOT diluted by
    document length, so a short probe strongly seeds a long matching cell
    (the knowledge store gets this for free because its chunks are short)."""
    text = re.sub(r"\s+", "", (query or "").lower())
    if len(text) < n:
        return doc_vec.get(text, 0.0) if text else 0.0
    qraw: dict[str, int] = {}
    for i in range(len(text) - n + 1):
        gram = text[i : i + n]
        qraw[gram] = qraw.get(gram, 0) + 1
    total = 0.0
    for gram, cnt in qraw.items():
        d = doc_vec.get(gram)
        if d:
            total += cnt * d
    return total


class HornetStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            c = self._con
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS hornet_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kb_item_id INTEGER,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    vec TEXT NOT NULL DEFAULT '{}',
                    phase TEXT NOT NULL DEFAULT '[0,0,0,0,0,0]',
                    x INTEGER NOT NULL DEFAULT 0,
                    y INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hornet_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    src INTEGER NOT NULL,
                    dst INTEGER NOT NULL,
                    relation TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    created_at REAL NOT NULL,
                    UNIQUE(src, dst, relation)
                );
                CREATE INDEX IF NOT EXISTS idx_edges_src ON hornet_edges(src);
                CREATE INDEX IF NOT EXISTS idx_edges_dst ON hornet_edges(dst);
                CREATE TABLE IF NOT EXISTS hornet_resonance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    hits TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hornet_emergent (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at REAL NOT NULL
                );
                """
            )
            try:
                c.execute(
                    "ALTER TABLE hornet_nodes ADD COLUMN z INTEGER NOT NULL DEFAULT 0"
                )
                c.commit()
            except sqlite3.OperationalError:
                pass  # column already present (fresh table has it, or upgraded)
            c.commit()

    # -- nodes ---------------------------------------------------------------
    def clear(self) -> None:
        with self._lock:
            self._con.execute("DELETE FROM hornet_edges")
            self._con.execute("DELETE FROM hornet_nodes")
            self._con.execute("DELETE FROM hornet_resonance")
            self._con.execute("DELETE FROM hornet_emergent")
            self._con.commit()

    def add_node(
        self,
        title: str,
        content: str = "",
        *,
        kb_item_id: Optional[int] = None,
        vec: Optional[dict[str, float]] = None,
        phase: Optional[list[float]] = None,
        x: int = 0,
        y: int = 0,
        z: int = 0,
    ) -> int:
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO hornet_nodes (kb_item_id, title, content, vec, phase, x, y, z, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    kb_item_id,
                    title,
                    content,
                    json.dumps(vec or {}, ensure_ascii=False),
                    json.dumps(phase or [0, 0, 0, 0, 0, 0], ensure_ascii=False),
                    x,
                    y,
                    z,
                    _now(),
                ),
            )
            self._con.commit()
            return int(cur.lastrowid)

    def list_nodes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT id, kb_item_id, title, content, vec, phase, x, y, z, created_at FROM hornet_nodes"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["vec"] = json.loads(d["vec"] or "{}")
            d["phase"] = json.loads(d["phase"] or "[0,0,0,0,0,0]")
            out.append(d)
        return out

    def node_count(self) -> int:
        with self._lock:
            return int(self._con.execute("SELECT COUNT(*) FROM hornet_nodes").fetchone()[0])

    # -- edges ---------------------------------------------------------------
    def add_edge(
        self, src: int, dst: int, relation: str, *, weight: float = 1.0,
        channel: Optional[str] = None,
    ) -> bool:
        if relation not in RELATIONS:
            return False
        ch = channel or RELATION_CHANNEL.get(relation, "D5")
        with self._lock:
            self._con.execute(
                "INSERT OR IGNORE INTO hornet_edges (src, dst, relation, channel, weight, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (src, dst, relation, ch, weight, _now()),
            )
            self._con.commit()
            return True

    def list_edges(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT src, dst, relation, channel, weight FROM hornet_edges"
            ).fetchall()
        return [dict(r) for r in rows]

    def edge_count(self) -> int:
        with self._lock:
            return int(self._con.execute("SELECT COUNT(*) FROM hornet_edges").fetchone()[0])

    # -- resonance -----------------------------------------------------------
    def record_resonance(self, query: str, hits: list[dict[str, Any]]) -> int:
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO hornet_resonance (query, hits, created_at) VALUES (?,?,?)",
                (query, json.dumps(hits, ensure_ascii=False), _now()),
            )
            self._con.commit()
            return int(cur.lastrowid)

    def recent_resonance(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT id, query, hits, created_at FROM hornet_resonance "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["hits"] = json.loads(d["hits"] or "[]")
            out.append(d)
        return out

    def node_hit_stats(self, limit: int = 200) -> dict[int, dict[str, float]]:
        """Aggregate per-node resonance stats (resonance_stat in the spec):
        hit_count, total amplitude, avg amplitude, and a load_factor = share of
        resonance runs in which this node resonated. Feeds cell fission, cavity
        detection and oscillation flags."""
        recs = self.recent_resonance(limit)
        if not recs:
            return {}
        hits: dict[int, dict[str, float]] = {}
        for rec in recs:
            for h in rec.get("hits", []):
                nid = h.get("node_id")
                if nid is None:
                    continue
                s = hits.setdefault(int(nid), {"hit_count": 0.0, "amp_sum": 0.0})
                s["hit_count"] += 1
                s["amp_sum"] += float(h.get("amplitude") or 0.0)
        runs = float(len(recs))
        for nid, s in hits.items():
            s["avg_amp"] = round(s["amp_sum"] / s["hit_count"], 4)
            s["load_factor"] = round(s["hit_count"] / runs, 4)
        return hits

    # -- emergence -----------------------------------------------------------
    def add_emergent(self, kind: str, title: str, detail: dict[str, Any]) -> tuple[int, bool]:
        """Insert an emergent product, deduped by (kind, title) so repeated
        auto-evolve runs don't re-notify the same finding. Returns (id, is_new)."""
        with self._lock:
            exists = self._con.execute(
                "SELECT id FROM hornet_emergent WHERE kind=? AND title=? LIMIT 1",
                (kind, title),
            ).fetchone()
            if exists:
                return int(exists[0]), False
            cur = self._con.execute(
                "INSERT INTO hornet_emergent (kind, title, detail, status, created_at) "
                "VALUES (?,?,?, 'new', ?)",
                (kind, title, json.dumps(detail, ensure_ascii=False), _now()),
            )
            self._con.commit()
            return int(cur.lastrowid), True

    def count_unread_emergent(self) -> int:
        with self._lock:
            return int(
                self._con.execute(
                    "SELECT COUNT(*) FROM hornet_emergent WHERE status='new'"
                ).fetchone()[0]
            )

    def list_emergent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT id, kind, title, detail, status, created_at FROM hornet_emergent "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"] or "{}")
            out.append(d)
        return out

    def set_emergent_status(self, eid: int, status: str) -> bool:
        with self._lock:
            cur = self._con.execute(
                "UPDATE hornet_emergent SET status=? WHERE id=?", (status, eid)
            )
            self._con.commit()
            return cur.rowcount > 0

    def emergent_count(self) -> int:
        with self._lock:
            return int(self._con.execute("SELECT COUNT(*) FROM hornet_emergent").fetchone()[0])


def _now() -> float:
    import time

    return time.time()


# re-export for builder/resonator
similarity = _similarity
