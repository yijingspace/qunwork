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
# Channel decay bias: D1/D4 causal converge faster, D0/D3 temporal wander more.
CHANNEL_DECAY = {"D0": 0.72, "D1": 0.62, "D2": 0.78, "D3": 0.72, "D4": 0.62, "D5": 0.85}

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
EMERGENT_KINDS = ("hypernode", "attractor", "gap")


def ngram_vector(text: str, n: int = 3) -> dict[str, float]:
    """Char n-gram count vector (normalized) — decent Chinese similarity."""
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
    ) -> int:
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO hornet_nodes (kb_item_id, title, content, vec, phase, x, y, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    kb_item_id,
                    title,
                    content,
                    json.dumps(vec or {}, ensure_ascii=False),
                    json.dumps(phase or [0, 0, 0, 0, 0, 0], ensure_ascii=False),
                    x,
                    y,
                    _now(),
                ),
            )
            self._con.commit()
            return int(cur.lastrowid)

    def list_nodes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT id, kb_item_id, title, content, vec, phase, x, y, created_at FROM hornet_nodes"
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

    # -- emergence -----------------------------------------------------------
    def add_emergent(self, kind: str, title: str, detail: dict[str, Any]) -> int:
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO hornet_emergent (kind, title, detail, status, created_at) "
                "VALUES (?,?,?, 'new', ?)",
                (kind, title, json.dumps(detail, ensure_ascii=False), _now()),
            )
            self._con.commit()
            return int(cur.lastrowid)

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
