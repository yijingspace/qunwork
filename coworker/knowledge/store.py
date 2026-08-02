"""Knowledge file library — workspace document indexing + manual entries.

SQLite-backed: `knowledge_items` (one row per document/entry) + `knowledge_chunks`
(content split into overlapping chunks, each with a vector for similarity search).

Retrieval uses an injectable embedder (cosine) with a char-n-gram cosine fallback
so Chinese text works out of the box without any external model.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

Embedder = Callable[[str], list[float]]

# Extensions we index + directories we never descend into.
# The authoritative route lives in coworker/knowledge/extractors (extract_text);
# this set mirrors it so scans skip unknown formats cheaply before reading.
_INDEX_EXTS = {".md", ".markdown", ".txt", ".rst", ".csv", ".log", ".json", ".pdf", ".docx"}
_SKIP_DIRS = {
    ".git",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    ".coworker",
    ".qunwork",
    ".state",
    ".tmp-state",
    "_swarm_reports",
    "target",
    "dist",
    "build",
    "site-packages",
    ".reasonix",
    "backups",
}
_CHUNK_SIZE = 600  # chars per chunk
_CHUNK_OVERLAP = 120


def _ngram_vector(text: str, n: int = 3) -> dict[str, float]:
    """Char n-gram count vector — decent Chinese similarity without an embedder."""
    text = re.sub(r"\s+", "", text.lower())
    if len(text) < n:
        return {text: 1.0} if text else {}
    counts: dict[str, int] = {}
    for i in range(len(text) - n + 1):
        gram = text[i : i + n]
        counts[gram] = counts.get(gram, 0) + 1
    norm = math.sqrt(sum(c * c for c in counts.values())) or 1.0
    return {g: c / norm for g, c in counts.items()}


def _cosine(a, b) -> float:
    if not a or not b:
        return 0.0
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a) & set(b)
        if not keys:
            return 0.0
        # Query-coverage similarity: the share of the QUERY's char n-grams present in the
        # document. Deliberately not length-normalized on the document side — a long doc
        # with the query's n-grams once would otherwise be diluted below any threshold,
        # which is fatal for short Chinese queries ("固态电池" → only 2 trigrams).
        # Query vectors are L2-normalized, so len(a) is the query's distinct n-gram count.
        return len(keys) / len(a)
    # dense float lists (real embedder)
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # prefer breaking at a newline near the boundary
            nl = text.rfind("\n", start + size // 2, end)
            if nl != -1 and nl > start:
                end = nl + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = end - overlap
    return [c for c in chunks if c]


class KnowledgeStore:
    def __init__(
        self,
        db_path: str | Path,
        embedder: Optional[Embedder] = None,
        workspace: Optional[str] = None,
    ) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder
        self._default_workspace = str(workspace) if workspace else None
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.execute(
            """CREATE TABLE IF NOT EXISTS knowledge_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'manual',   -- manual | file
                source_path TEXT,
                title TEXT,
                fingerprint TEXT,                       -- mtime+size for file items
                created_at REAL,
                updated_at REAL,
                UNIQUE (workspace, source_path)
            )"""
        )
        self._con.execute(
            """CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                vector TEXT NOT NULL
            )"""
        )
        self._con.execute("CREATE INDEX IF NOT EXISTS ix_chunks_item ON knowledge_chunks(item_id)")
        self._con.commit()

    # -- embed -----------------------------------------------------------------
    def _embed(self, text: str):
        if self._embedder is not None:
            try:
                return self._embedder(text)
            except Exception:
                pass
        return _ngram_vector(text)

    # -- indexing --------------------------------------------------------------
    def add_text(self, title: str, content: str, *, kind: str = "manual", workspace: Optional[str] = None) -> int:
        """Index a free-text entry (manual knowledge). Returns the item id."""
        if not title or not content:
            raise ValueError("title and content are required")
        ws = str(workspace) if workspace else (self._default_workspace or "")
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO knowledge_items (workspace, kind, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                (ws, kind, title, time.time(), time.time()),
            )
            item_id = cur.lastrowid
            self._index_chunks(item_id, title, content)
            self._con.commit()
        return item_id

    def index_file(self, path: str | Path, *, workspace: Optional[str] = None, force: bool = False) -> Optional[int]:
        """Index one document file (md/txt/pdf/docx/...). Re-indexes only when the
        file changed (mtime+size fingerprint). Returns the item id, or None when
        skipped (unknown format, unreadable, or extraction failure)."""
        p = Path(path)
        if not p.is_file() or p.suffix.lower() not in _INDEX_EXTS:
            return None
        ws = (
            str(workspace)
            if workspace
            else (self._default_workspace or str(p.parent))
        )
        try:
            from .extractors import ExtractionError, UnsupportedFormatError, extract_text

            content, _fmt = extract_text(p)
        except UnsupportedFormatError:
            return None  # unknown format: skip silently
        except (ExtractionError, OSError):
            # Extraction failures must NOT be silently dropped: scans count them
            # as failed so the caller can surface the reason (e.g. a scanned PDF
            # with no text layer, an encrypted file, a corrupt document).
            raise
        fp = f"{p.stat().st_mtime_ns}:{p.stat().st_size}"
        with self._lock:
            row = self._con.execute(
                "SELECT id, fingerprint FROM knowledge_items WHERE workspace=? AND source_path=?",
                (ws, str(p)),
            ).fetchone()
            if row and row[1] == fp and not force:
                return row[0]
            if row:
                self._con.execute("DELETE FROM knowledge_chunks WHERE item_id=?", (row[0],))
                item_id = row[0]
                self._con.execute(
                    "UPDATE knowledge_items SET title=?, fingerprint=?, updated_at=? WHERE id=?",
                    (p.stem, fp, time.time(), item_id),
                )
            else:
                cur = self._con.execute(
                    "INSERT INTO knowledge_items (workspace, kind, source_path, title, fingerprint, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (ws, "file", str(p), p.stem, fp, time.time(), time.time()),
                )
                item_id = cur.lastrowid
            self._index_chunks(item_id, p.stem, content)
            self._con.commit()
        return item_id

    def scan_workspace(self, workspace: Optional[str] = None) -> dict:
        """Index every md/txt document under the workspace. Returns a summary
        (added = new files indexed, updated = changed files re-indexed, skipped = unchanged)."""
        ws = str(workspace) if workspace else self._default_workspace
        if not ws:
            return {"added": 0, "updated": 0, "skipped": 0, "failed": 0}
        return self._scan_tree(Path(ws), ws)

    def index_folder(
        self,
        folder: str | Path,
        *,
        workspace: Optional[str] = None,
        max_files: Optional[int] = 20000,
        max_total_bytes: Optional[int] = 2 * 1024 * 1024 * 1024,
    ) -> dict:
        """Index every md/txt document under an arbitrary LOCAL folder (which may
        live outside the workspace). Files are chunked + vectorized into the
        knowledge library; the original path is kept as source_path. Re-scanning
        the same folder is idempotent (fingerprint dedup). `max_files` /
        `max_total_bytes` guard against accidentally importing an enormous tree
        (e.g. a home directory); when the cap is hit the scan STOPS and reports
        `truncated` so the caller knows the folder wasn't fully imported."""
        root = Path(folder).resolve()
        if not root.is_dir():
            return {"added": 0, "updated": 0, "skipped": 0, "failed": 1}
        ws = str(workspace) if workspace else (self._default_workspace or "")
        return self._scan_tree(
            root, ws, max_files=max_files, max_total_bytes=max_total_bytes
        )

    def _scan_tree(
        self,
        root: Path,
        ws: str,
        *,
        max_files: Optional[int] = None,
        max_total_bytes: Optional[int] = None,
    ) -> dict:
        """Shared scan driver: fingerprint-snapshot dedup + per-file re-index."""
        with self._lock:
            snapshot = {
                src: fp
                for src, fp in self._con.execute(
                    "SELECT source_path, fingerprint FROM knowledge_items WHERE workspace=?",
                    (ws,),
                ).fetchall()
            }
        added = skipped = failed = updated = 0
        processed = total_bytes = 0
        truncated = False
        failures: list[dict[str, str]] = []
        skip_reasons: dict[str, int] = {}
        for p in root.rglob("*"):
            if p.is_dir() or p.suffix.lower() not in _INDEX_EXTS:
                if not p.is_dir():
                    ext = p.suffix.lower() or "(none)"
                    key = f"unsupported format {ext}"
                    skip_reasons[key] = skip_reasons.get(key, 0) + 1
                continue
            rel = p.relative_to(root)
            if any(part in _SKIP_DIRS for part in rel.parts):
                skip_reasons["excluded directory"] = skip_reasons.get("excluded directory", 0) + 1
                continue
            if max_files is not None and processed >= max_files:
                truncated = True
                break
            try:
                st = p.stat()
                if max_total_bytes is not None and total_bytes >= max_total_bytes:
                    truncated = True
                    break
                total_bytes += st.st_size
                processed += 1
                fp = f"{st.st_mtime_ns}:{st.st_size}"
            except OSError:
                failed += 1
                if len(failures) < 50:
                    failures.append({"path": str(p), "reason": "cannot stat file"})
                continue
            if snapshot.get(str(p)) == fp:
                skip_reasons["unchanged"] = skip_reasons.get("unchanged", 0) + 1
                skipped += 1
                continue
            is_update = str(p) in snapshot
            try:
                self.index_file(p, workspace=ws, force=True)
                if is_update:
                    updated += 1
                else:
                    added += 1
            except Exception as exc:
                failed += 1
                if len(failures) < 50:
                    failures.append({"path": str(p), "reason": str(exc)[:200]})
        return {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
            "truncated": truncated,
            "failures": failures,
            "skip_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: -kv[1])),
        }

    def delete(self, item_id: int) -> bool:
        with self._lock:
            cur = self._con.execute("DELETE FROM knowledge_items WHERE id=?", (item_id,))
            self._con.execute("DELETE FROM knowledge_chunks WHERE item_id=?", (item_id,))
            self._con.commit()
        return cur.rowcount > 0

    def count_items(self, workspace: Optional[str] = None) -> int:
        """Total number of knowledge items for the workspace (or all workspaces)."""
        ws = str(workspace) if workspace else self._default_workspace
        with self._lock:
            if ws:
                row = self._con.execute(
                    "SELECT COUNT(*) FROM knowledge_items WHERE workspace=?", (ws,)
                ).fetchone()
            else:
                row = self._con.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()
        return row[0] if row else 0

    def list_items(
        self,
        workspace: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        ws = str(workspace) if workspace else self._default_workspace
        with self._lock:
            if ws:
                rows = self._con.execute(
                    "SELECT id, kind, source_path, title, created_at, updated_at FROM knowledge_items "
                    "WHERE workspace=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (ws, limit, offset),
                ).fetchall()
            else:
                rows = self._con.execute(
                    "SELECT id, kind, source_path, title, created_at, updated_at FROM knowledge_items "
                    "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [
            {
                "id": r[0],
                "kind": r[1],
                "source_path": r[2],
                "title": r[3],
                "created_at": r[4],
                "updated_at": r[5],
            }
            for r in rows
        ]

    # -- search ----------------------------------------------------------------
    def search(
        self,
        query: str,
        k: int = 5,
        workspace: Optional[str] = None,
        min_score: float = 0.1,
    ) -> list[dict]:
        """Top-k ITEMS matching the query (best chunk per item), with metadata.

        `min_score` filters out weak n-gram matches; per-item dedup keeps a long
        document's many chunks from crowding out the whole result list.
        """
        qv = self._embed(query)
        ws = str(workspace) if workspace else self._default_workspace
        with self._lock:
            if ws:
                rows = self._con.execute(
                    """SELECT c.item_id, c.chunk_index, c.content, c.vector, i.kind, i.title, i.source_path
                       FROM knowledge_chunks c JOIN knowledge_items i ON i.id = c.item_id
                       WHERE i.workspace=? ORDER BY c.id""",
                    (ws,),
                ).fetchall()
            else:
                rows = self._con.execute(
                    """SELECT c.item_id, c.chunk_index, c.content, c.vector, i.kind, i.title, i.source_path
                       FROM knowledge_chunks c JOIN knowledge_items i ON i.id = c.item_id
                       ORDER BY c.id"""
                ).fetchall()
        scored: list[tuple[float, dict]] = []
        for item_id, ci, content, vec_json, kind, title, src in rows:
            try:
                vec = json.loads(vec_json)
            except Exception:
                continue
            score = _cosine(qv, vec)
            if score > 0:
                scored.append(
                    (
                        score,
                        {
                            "item_id": item_id,
                            "chunk_index": ci,
                            "content": content,
                            "score": round(score, 4),
                            "kind": kind,
                            "title": title,
                            "source_path": src,
                        },
                    )
                )
        scored.sort(key=lambda t: -t[0])
        seen: set[int] = set()
        results: list[dict] = []
        for _, hit in scored:
            if hit["score"] < min_score:
                continue
            if hit["item_id"] in seen:
                continue
            seen.add(hit["item_id"])
            results.append(hit)
            if len(results) >= k:
                break
        return results

    # -- internals -------------------------------------------------------------
    def _index_chunks(self, item_id: int, title: str, content: str) -> None:
        chunks = _chunk_text(content)
        if not chunks:
            return
        for i, chunk in enumerate(chunks):
            vec = self._embed(chunk)
            self._con.execute(
                "INSERT INTO knowledge_chunks (item_id, chunk_index, content, vector) VALUES (?,?,?,?)",
                (item_id, i, chunk, json.dumps(vec)),
            )

    def close(self) -> None:
        with self._lock:
            try:
                self._con.close()
            except sqlite3.Error:
                pass
