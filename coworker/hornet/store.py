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
from typing import Any, Iterable, Optional

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


_NODE_COLS = (
    "id", "kb_item_id", "title", "content", "vec", "phase", "x", "y", "z",
    "topo", "failed_count", "freshness", "created_at",
)


def _node_select(fields: Optional[Iterable[str]] = None) -> str:
    """SQL projection for hornet_nodes: subset of _NODE_COLS (whitelisted, in
    table order) with the COALESCE defaults preserved. Unknown names are dropped."""
    chosen = [c for c in (fields or _NODE_COLS) if c in _NODE_COLS] or list(_NODE_COLS)
    expr = {c: c for c in _NODE_COLS}
    expr["failed_count"] = "COALESCE(failed_count, 0) AS failed_count"
    expr["freshness"] = "COALESCE(freshness, 1.0) AS freshness"
    return ", ".join(expr[c] for c in chosen)


def _parse_node_row(r) -> dict[str, Any]:
    """sqlite row -> node dict, JSON-decoding only the columns present (the
    projected-away vec/phase/topo stay absent instead of parsed)."""
    d = dict(r)
    if "vec" in d:
        d["vec"] = json.loads(d["vec"] or "{}")
    if "phase" in d:
        d["phase"] = json.loads(d["phase"] or "[0,0,0,0,0,0]")
    if "topo" in d:
        d["topo"] = json.loads(d["topo"] or "[]") if d.get("topo") else []
    return d


class HornetStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        """Explicitly close the SQLite connection (call on shutdown)."""
        with self._lock:
            self._con.close()

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
            try:
                c.execute("ALTER TABLE hornet_nodes ADD COLUMN topo TEXT")
                c.commit()
            except sqlite3.OperationalError:
                pass
            try:
                c.execute(
                    "ALTER TABLE hornet_nodes ADD COLUMN failed_count INTEGER NOT NULL DEFAULT 0"
                )
                c.commit()
            except sqlite3.OperationalError:
                pass
            try:
                c.execute(
                    "ALTER TABLE hornet_nodes ADD COLUMN freshness REAL NOT NULL DEFAULT 1.0"
                )
                c.commit()
            except sqlite3.OperationalError:
                pass
            c.commit()

    # -- nodes ---------------------------------------------------------------
    def clear(self) -> None:
        with self._lock:
            self._con.execute("DELETE FROM hornet_edges")
            self._con.execute("DELETE FROM hornet_nodes")
            self._con.execute("DELETE FROM hornet_resonance")
            self._con.execute("DELETE FROM hornet_emergent")
            self._con.commit()

    def commit(self) -> None:
        """Commit a batch started with commit=False (see add_node/add_edge)."""
        with self._lock:
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
        topo: Optional[list[float]] = None,
        commit: bool = True,
    ) -> int:
        """Insert a hive cell. Pass commit=False for bulk inserts (a rebuild
        writes thousands of cells) and call commit() once at the end — per-row
        commits were ~N disk transactions per rebuild."""
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO hornet_nodes (kb_item_id, title, content, vec, phase, x, y, z, topo, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    kb_item_id,
                    title,
                    content,
                    json.dumps(vec or {}, ensure_ascii=False),
                    json.dumps(phase or [0, 0, 0, 0, 0, 0], ensure_ascii=False),
                    x,
                    y,
                    z,
                    json.dumps(topo or [], ensure_ascii=False),
                    _now(),
                ),
            )
            if commit:
                self._con.commit()
            return int(cur.lastrowid)

    def list_nodes(self, *, fields: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
        """Load hive cells. Pass `fields` to project away the heavy JSON blobs
        (vec/phase/topo): the 6h auto-evolve + freshness sweeps on a ~2.4k-cell
        hive once materialized ~1.5GB of parsed n-gram vectors and OOM-killed
        the whole sidecar mid-task (silent death, no traceback). Callers that
        don't do vector math must project: evolve/freshness/health/graph only
        need id/title/content/coords. Default keeps the full row."""
        with self._lock:
            rows = self._con.execute(
                f"SELECT {_node_select(fields)} FROM hornet_nodes"
            ).fetchall()
        return [_parse_node_row(r) for r in rows]

    def list_titles(self) -> list[str]:
        """Titles only — for fission-dedup / known-title checks that must NOT
        pay the content/vec load (see list_nodes)."""
        with self._lock:
            rows = self._con.execute("SELECT title FROM hornet_nodes").fetchall()
        return [r["title"] for r in rows]

    def iter_nodes(self, *, fields: Optional[Iterable[str]] = None, batch: int = 100):
        """Yield node dicts in id-ordered batches (generator). Use this when a
        caller needs the heavy vec/content of every cell but only transiently
        (e.g. resonate's seed pass): materializing all of them at once via
        list_nodes() is the historical ~1.5GB OOM. Rows are fetched under the
        store lock and yielded outside it, so writes between batches are safe
        (keyset pagination on id)."""
        sel = _node_select(fields)
        last_id = -1
        while True:
            with self._lock:
                rows = self._con.execute(
                    f"SELECT {sel} FROM hornet_nodes WHERE id > ? ORDER BY id LIMIT ?",
                    (last_id, batch),
                ).fetchall()
            if not rows:
                return
            for r in rows:
                d = _parse_node_row(r)
                last_id = d["id"]
                yield d

    def node_count(self) -> int:
        with self._lock:
            return int(self._con.execute("SELECT COUNT(*) FROM hornet_nodes").fetchone()[0])

    def set_freshness(self, node_id: int, freshness: float) -> bool:
        """Update a node's freshness score (A: 周期驱动的知识保鲜与遗忘)."""
        with self._lock:
            cur = self._con.execute(
                "UPDATE hornet_nodes SET freshness=? WHERE id=?",
                (round(max(0.0, min(1.0, freshness)), 4), node_id),
            )
            self._con.commit()
            return cur.rowcount > 0

    def batch_freshness(self, updates: dict[int, float]) -> int:
        """Apply many freshness updates in ONE transaction (A: freshness_pass
        visits every node — per-node commits were up to 2N transactions)."""
        if not updates:
            return 0
        with self._lock:
            rows = 0
            for nid, f in updates.items():
                cur = self._con.execute(
                    "UPDATE hornet_nodes SET freshness=? WHERE id=?",
                    (round(max(0.0, min(1.0, f)), 4), nid),
                )
                rows += cur.rowcount
            self._con.commit()
            return rows

    def set_z(self, node_id: int, z: int) -> bool:
        """Move a node to a different zone layer. Deprecated for freshness
        downgrade — use freshness (reversible); z moves are reserved for build/import."""
        with self._lock:
            cur = self._con.execute(
                "UPDATE hornet_nodes SET z=? WHERE id=?", (z, node_id)
            )
            self._con.commit()
            return cur.rowcount > 0

    # -- edges ---------------------------------------------------------------
    def add_edge(
        self, src: int, dst: int, relation: str, *, weight: float = 1.0,
        channel: Optional[str] = None, commit: bool = True,
    ) -> bool:
        """Insert a relation edge. Pass commit=False for bulk inserts (a rebuild
        writes tens of thousands of edges) and call commit() once at the end."""
        if relation not in RELATIONS:
            return False
        ch = channel or RELATION_CHANNEL.get(relation, "D5")
        with self._lock:
            self._con.execute(
                "INSERT OR IGNORE INTO hornet_edges (src, dst, relation, channel, weight, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (src, dst, relation, ch, weight, _now()),
            )
            if commit:
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

    # -- cognitive-action loop feedback (#2) ---------------------------------
    def feedback(
        self,
        node_ids: list[int],
        success: bool,
        query_phase: Optional[list[float]] = None,
        *,
        alpha: float = 0.15,
    ) -> int:
        """Action result modulates the knowledge topology.

        success=True  → nudge each node's phase toward the query phase
                         (crystallize the resonance attractor).
        success=False → push each node to the Z- traceback layer
                         (mark as risk/evidence zone for conflict detection).

        Returns the number of nodes actually updated.
        """
        if not node_ids:
            return 0
        updated = 0
        with self._lock:
            for nid in node_ids:
                row = self._con.execute(
                    "SELECT phase, z FROM hornet_nodes WHERE id=?", (nid,)
                ).fetchone()
                if not row:
                    continue
                if success and query_phase:
                    phase = json.loads(row["phase"] or "[0,0,0,0,0,0]")
                    qp = query_phase
                    n = max(len(phase), len(qp))
                    pa = (phase + [0.0] * n)[:n]
                    qa = (qp + [0.0] * n)[:n]
                    new_phase = [round(pa[i] + alpha * (qa[i] - pa[i]), 4) for i in range(n)]
                    norm = math.sqrt(sum(p * p for p in new_phase)) or 1.0
                    new_phase = [round(p / norm, 4) for p in new_phase]
                    self._con.execute(
                        "UPDATE hornet_nodes SET phase=? WHERE id=?",
                        (json.dumps(new_phase, ensure_ascii=False), nid),
                    )
                elif not success:
                    # Reversible feedback (review fix): a failed run must NOT
                    # permanently rewrite the 3D layout / cross-layer damping by
                    # forcing z=-1 — a single model timeout would then reshape
                    # the whole hive forever. Track a failure counter instead;
                    # the observer's conflict/cavity logic can read it later.
                    self._con.execute(
                        "UPDATE hornet_nodes SET failed_count = failed_count + 1 WHERE id=?",
                        (nid,),
                    )
                updated += 1
            self._con.commit()
        return updated

    # -- C: 跨组织蜂巢共振对齐 (export/import) -------------------------------
    def export_hive(self) -> dict[str, Any]:
        """Export the hive topology (nodes with phase + edges) for cross-org
        resonance alignment. Content is excluded (IP/privacy); only the
        structural fingerprint travels."""
        nodes = self.list_nodes(fields=("id", "title", "phase", "x", "y", "z", "freshness"))
        edges = self.list_edges()
        return {
            "nodes": [
                {
                    "id": n["id"],  # real id — import remaps edges by this
                    "title": n["title"],
                    "phase": n["phase"],
                    "x": n["x"], "y": n["y"], "z": n["z"],
                    "freshness": n.get("freshness", 1.0),
                }
                for n in nodes
            ],
            "edges": [
                {"src": e["src"], "dst": e["dst"], "relation": e["relation"],
                 "channel": e["channel"], "weight": e["weight"]}
                for e in edges
            ],
        }

    def import_hive(self, payload: dict[str, Any], *, phase_conflict_threshold: float = 0.7) -> dict[str, Any]:
        """Import a remote hive and align with the local one.

        For each imported node:
        - If a local node with the same title exists, compare phases. If they
          conflict (phase distance > threshold), add an `opposite` edge and
          record a `conflict` emergent.
        - If no local match, add the node as a new cell (cross-org knowledge).

        Edge IDs are remapped to the local node IDs. Returns a summary.
        """
        remote_nodes = payload.get("nodes", [])
        remote_edges = payload.get("edges", [])
        # fields 必须带 phase: 下面 _phase_distance(local["phase"], ...) 要用本地
        # 相位对冲 (缺了就 KeyError, 崩在 "malformed skip" 注释保护的块里)。
        local_nodes = self.list_nodes(fields=("id", "title", "phase"))
        local_by_title = {n["title"]: n for n in local_nodes}
        id_map: dict[int, int] = {}
        imported = conflicts = 0
        for rn in remote_nodes:
            title = str(rn.get("title") or f"remote_{i}")
            phase = rn.get("phase") or [0.0] * 6
            if not isinstance(phase, list) or not all(isinstance(p, (int, float)) for p in phase):
                continue  # malformed payload — skip, never crash the import
            rid = rn.get("id")
            local = local_by_title.get(title)
            if local:
                if isinstance(rid, int):
                    id_map[rid] = local["id"]
                d = _phase_distance(local["phase"], phase)
                if d > phase_conflict_threshold:
                    self.add_edge(local["id"], local["id"], "opposite", weight=0.5, channel="G8")
                    self.add_emergent(
                        "conflict",
                        f"跨组织冲突: {title[:40]}",
                        {"local": local["id"], "remote_phase": phase, "phase_dist": round(d, 4),
                         "hint": "导入蜂巢与本地相位对冲,已标记对立边"},
                    )
                    conflicts += 1
            else:
                nid = self.add_node(
                    title, content="", vec=ngram_vector(title),
                    phase=phase, x=rn.get("x", 0), y=rn.get("y", 0), z=rn.get("z", 0),
                )
                if isinstance(rid, int):
                    id_map[rid] = nid
                imported += 1
        edges_added = 0
        for re in remote_edges:
            src = id_map.get(re.get("src"))
            dst = id_map.get(re.get("dst"))
            if src and dst and src != dst:
                if self.add_edge(src, dst, re.get("relation", "similar"),
                                 weight=re.get("weight", 1.0), channel=re.get("channel")):
                    edges_added += 1
        return {"imported": imported, "conflicts": conflicts, "edges_added": edges_added}


def _now() -> float:
    import time

    return time.time()


def _phase_distance(a: list[float], b: list[float]) -> float:
    """Normalized phase distance in [0, ~1] for cross-org conflict detection."""
    if not a or not b:
        return 1.0
    n = max(len(a), len(b))
    pa = (a + [0.0] * n)[:n]
    pb = (b + [0.0] * n)[:n]
    d = math.sqrt(sum((x - y) ** 2 for x, y in zip(pa, pb)))
    return d / 2.0


# re-export for builder/resonator
similarity = _similarity
