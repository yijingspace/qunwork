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
    # Obsidian app metadata — `.obsidian/` holds UI preferences (graph.json,
    # workspace.json, hotkeys.json…), NOT documents. Indexing them pollutes the
    # knowledge library with config files that have no semantic content (they
    # show up as weird orphan nodes like title="graph").
    ".obsidian",
}
# Obsidian (and other tools') per-folder UI-config JSON that a scan could still
# reach even with `.obsidian` skipped (a stray config copied next to docs, or a
# vault layout where .obsidian lives elsewhere). These carry zero knowledge.
_OBSIDIAN_CONFIG_JSON = {
    "graph.json",
    "workspace.json",
    "workspace-mobile.json",
    "hotkeys.json",
    "app.json",
    "appearance.json",
    "community-plugins.json",
    "core-plugins.json",
    "daily-notes.json",
    "templates.json",
    "bookmarks.json",
    "web-clipper.json",
    "publish.json",
    "sync.json",
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
                kind TEXT NOT NULL DEFAULT 'manual',   -- manual | file | automation | swarm_report
                source_path TEXT,
                source_run_id TEXT,                    -- asset cross-index: which run sunk this entry
                title TEXT,
                fingerprint TEXT,                       -- mtime+size for file items
                created_at REAL,
                updated_at REAL,
                UNIQUE (workspace, source_path)
            )"""
        )
        try:
            self._con.execute("ALTER TABLE knowledge_items ADD COLUMN source_run_id TEXT")
            self._con.commit()
        except sqlite3.OperationalError:
            pass  # column already present
        # Asset lifecycle (Phase 3): usage counter feeds governance feedback;
        # retired entries are hidden from search/list but kept for audit.
        try:
            self._con.execute("ALTER TABLE knowledge_items ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0")
            self._con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            self._con.execute("ALTER TABLE knowledge_items ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")
            self._con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            # Research-relay chain: derived entries point at the knowledge item
            # they were researched from (knowledge → research → new knowledge).
            self._con.execute("ALTER TABLE knowledge_items ADD COLUMN parent_id INTEGER")
            self._con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            # S11 版本控制: 当前版本号 (更新/重索引时 +1, 旧内容快照历史)。
            self._con.execute(
                "ALTER TABLE knowledge_items ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
            )
            self._con.commit()
        except sqlite3.OperationalError:
            pass
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
        # S11 知识资产版本控制 (蜂群审计报告 G5): 每次更新/重索引把旧内容
        # 快照进 knowledge_history, 支持审计与回滚 (rollback), 误删可回退。
        self._con.execute(
            """CREATE TABLE IF NOT EXISTS knowledge_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                title TEXT,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
        self._con.execute(
            "CREATE INDEX IF NOT EXISTS ix_kh_item ON knowledge_history(item_id, version)"
        )
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
    def add_text(
        self,
        title: str,
        content: str,
        *,
        kind: str = "manual",
        workspace: Optional[str] = None,
        source_run_id: Optional[str] = None,
        parent_id: Optional[int] = None,
    ) -> int:
        """Index a free-text entry (manual knowledge). Returns the item id.
        parent_id: the knowledge item this entry was researched/derived from
        (research-relay chain: knowledge → research → new knowledge)."""
        if not title or not content:
            raise ValueError("title and content are required")
        ws = str(workspace) if workspace else (self._default_workspace or "")
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO knowledge_items (workspace, kind, source_run_id, parent_id, title, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (ws, kind, source_run_id, parent_id, title, time.time(), time.time()),
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
                "SELECT id, fingerprint, COALESCE(version, 1) FROM knowledge_items "
                "WHERE workspace=? AND source_path=?",
                (ws, str(p)),
            ).fetchone()
            if row and row[1] == fp and not force:
                return row[0]
            if row:
                # S11: 更新前把旧内容快照进历史版本链 (误删可回退)。
                old_chunks = self._con.execute(
                    "SELECT content FROM knowledge_chunks WHERE item_id=? ORDER BY chunk_index",
                    (row[0],),
                ).fetchall()
                old_content = "\n".join(c[0] for c in old_chunks) if old_chunks else ""
                old_version = int(row[2]) if len(row) > 2 and row[2] else 1
                if old_content:
                    self._snapshot_history(row[0], p.stem, old_content, old_version)
                self._con.execute("DELETE FROM knowledge_chunks WHERE item_id=?", (row[0],))
                item_id = row[0]
                self._con.execute(
                    "UPDATE knowledge_items SET title=?, fingerprint=?, updated_at=?, version=version+1 WHERE id=?",
                    (p.stem, fp, time.time(), item_id),
                )
                self._index_chunks(item_id, p.stem, content)
                self._con.commit()
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
        """Index every md/txt document under the workspace(s). Returns a summary
        (added = new files indexed, updated = changed files re-indexed, skipped = unchanged).

        When `workspace` is omitted, scans EVERY directory that has already been
        indexed (from `source_path` of existing file items) plus the default
        workspace — fixing "scan shows 0 added" when no default is set: the
        library is multi-workspace, so the scan must be too, not silently no-op."""
        if workspace:
            ws = str(workspace)
            return self._scan_tree(Path(ws), ws)
        targets = self._scan_targets()
        if not targets:
            return {"added": 0, "updated": 0, "skipped": 0, "failed": 0}
        total = {"added": 0, "updated": 0, "skipped": 0, "failed": 0, "failures": []}
        for target in targets:
            part = self._scan_tree(target["path"], target["ws"])
            for k in ("added", "updated", "skipped", "failed"):
                total[k] += part.get(k, 0)
            total["failures"].extend(part.get("failures", []) or [])
        total["workspaces_scanned"] = len(targets)
        return total

    def _scan_targets(self) -> list[dict]:
        """Directories to scan when no explicit workspace is given: the default
        workspace (if set) plus every parent directory of an already-indexed
        file item. Deduped by resolved path, skipping _SKIP_DIRS members."""
        seen: dict[str, str] = {}  # resolved path -> workspace label
        if self._default_workspace:
            seen[str(Path(self._default_workspace).resolve())] = self._default_workspace
        try:
            rows = self._con.execute(
                "SELECT DISTINCT source_path FROM knowledge_items WHERE kind='file'"
                " AND source_path IS NOT NULL AND source_path != ''"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for (sp,) in rows:
            try:
                d = Path(sp).parent
                if any(part in _SKIP_DIRS for part in d.parts):
                    continue
                key = str(d.resolve())
                if key not in seen:
                    seen[key] = str(d)
            except (OSError, ValueError):
                continue
        return [
            {"path": Path(ws), "ws": ws}
            for ws in sorted(seen.values())
            if Path(ws).is_dir()
        ]

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
            # Obsidian UI-config JSON (graph.json & co.) carries no knowledge even
            # when it sits outside a skipped `.obsidian` dir — never index it.
            if p.suffix.lower() == ".json" and p.name in _OBSIDIAN_CONFIG_JSON:
                skip_reasons["obsidian config json"] = (
                    skip_reasons.get("obsidian config json", 0) + 1
                )
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

    def set_retired(self, item_id: int, retired: bool) -> bool:
        """Asset lifecycle (Phase 3): mark an entry retired (hidden from search)
        or restore it. Audit trail is preserved — retirement is not deletion."""
        with self._lock:
            cur = self._con.execute(
                "UPDATE knowledge_items SET retired = ? WHERE id = ?",
                (1 if retired else 0, item_id),
            )
            self._con.commit()
        return cur.rowcount > 0

    def item_content(self, item_id: int) -> str:
        """Reassemble an item's full text from its chunks (HORNET builder needs
        the body, which list_items deliberately omits)."""
        with self._lock:
            rows = self._con.execute(
                "SELECT content FROM knowledge_chunks WHERE item_id=? ORDER BY chunk_index",
                (item_id,),
            ).fetchall()
        return "\n".join(r[0] for r in rows)

    def get_item_meta(self, item_id: int) -> Optional[dict]:
        """One item's metadata row (no body) — for cross-referencing a HORNET
        node back to its source knowledge entry (问题3: 共振命中→原文)."""
        with self._lock:
            row = self._con.execute(
                "SELECT id, kind, source_path, source_run_id, parent_id, title,"
                " created_at, updated_at, use_count, retired, workspace"
                " FROM knowledge_items WHERE id=?",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "kind": row[1],
            "source_path": row[2],
            "source_run_id": row[3],
            "parent_id": row[4],
            "title": row[5],
            "created_at": row[6],
            "updated_at": row[7],
            "use_count": row[8],
            "retired": row[9],
            "workspace": row[10],
        }

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
                    "SELECT id, kind, source_path, source_run_id, parent_id, title, created_at, updated_at, use_count, retired FROM knowledge_items "
                    "WHERE workspace=? AND retired=0 ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (ws, limit, offset),
                ).fetchall()
            else:
                rows = self._con.execute(
                    "SELECT id, kind, source_path, source_run_id, parent_id, title, created_at, updated_at, use_count, retired FROM knowledge_items "
                    "WHERE retired=0 ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [
            {
                "id": r[0],
                "kind": r[1],
                "source_path": r[2],
                "source_run_id": r[3],
                "parent_id": r[4],
                "title": r[5],
                "created_at": r[6],
                "updated_at": r[7],
                "use_count": r[8],
                "retired": r[9],
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
                    """SELECT c.item_id, c.chunk_index, c.content, c.vector, i.kind, i.title, i.source_path, i.source_run_id, i.use_count
                       FROM knowledge_chunks c JOIN knowledge_items i ON i.id = c.item_id
                       WHERE i.workspace=? AND i.retired=0 ORDER BY c.id""",
                    (ws,),
                ).fetchall()
            else:
                rows = self._con.execute(
                    """SELECT c.item_id, c.chunk_index, c.content, c.vector, i.kind, i.title, i.source_path, i.source_run_id, i.use_count
                       FROM knowledge_chunks c JOIN knowledge_items i ON i.id = c.item_id
                       WHERE i.retired=0 ORDER BY c.id"""
                ).fetchall()
        scored: list[tuple[float, dict]] = []
        for item_id, ci, content, vec_json, kind, title, src, src_run, use_count in rows:
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
                            "source_run_id": src_run,
                            "use_count": use_count,
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
        # Asset lifecycle (Phase 3): a retrieval ticks the usage counter — the
        # governance feedback signal for the asset loop (used assets rank better).
        if results:
            with self._lock:
                self._con.executemany(
                    "UPDATE knowledge_items SET use_count = use_count + 1 WHERE id = ?",
                    [(r["item_id"],) for r in results],
                )
                self._con.commit()
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

    # -- S11 知识资产版本控制 (蜂群审计报告 G5) ------------------------------
    def _snapshot_history(self, item_id: int, title: str, content: str, version: int) -> None:
        """把当前内容快照进 knowledge_history (更新/重索引前调用)。"""
        chunks = _chunk_text(content)
        snapshot = "\n".join(chunks) if chunks else content
        self._con.execute(
            "INSERT INTO knowledge_history (item_id, version, title, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (item_id, version, title, snapshot, time.time()),
        )

    def history(self, item_id: int, limit: int = 50) -> list[dict]:
        """版本链: 该知识条目的历史版本 (旧->新, 含当前版本标记)。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT version, title, content, created_at FROM knowledge_history "
                "WHERE item_id=? ORDER BY version DESC LIMIT ?",
                (item_id, limit),
            ).fetchall()
            cur = self._con.execute(
                "SELECT version, title FROM knowledge_items WHERE id=?",
                (item_id,),
            ).fetchone()
        out = [
            {"version": r[0], "title": r[1], "content": r[2], "created_at": r[3], "current": False}
            for r in rows
        ]
        if cur is not None:
            out.insert(
                0,
                {
                    "version": cur[0],
                    "title": cur[1],
                    "content": "（当前版本内容, 见 knowledge_chunks）",
                    "created_at": None,
                    "current": True,
                },
            )
        return out

    def rollback(self, item_id: int, version: int) -> bool:
        """回滚到指定历史版本: 恢复该版本内容并快照当前内容, 版本 +1。
        返回是否成功; 目标版本不存在返回 False (不改动)。"""
        with self._lock:
            cur = self._con.execute(
                "SELECT version, title FROM knowledge_items WHERE id=?", (item_id,)
            ).fetchone()
            if cur is None:
                return False
            hist = self._con.execute(
                "SELECT title, content FROM knowledge_history "
                "WHERE item_id=? AND version=?",
                (item_id, version),
            ).fetchone()
            if hist is None:
                return False
            # 快照当前内容 → 历史, 然后恢复目标版本
            current_title = cur[1]
            chunks = self._con.execute(
                "SELECT content FROM knowledge_chunks WHERE item_id=? ORDER BY chunk_index",
                (item_id,),
            ).fetchall()
            current_content = "\n".join(c[0] for c in chunks)
            self._snapshot_history(item_id, current_title, current_content, cur[0])
            self._con.execute(
                "DELETE FROM knowledge_chunks WHERE item_id=?", (item_id,)
            )
            new_title = hist[0] or current_title
            self._con.execute(
                "UPDATE knowledge_items SET title=?, version=version+1, updated_at=? WHERE id=?",
                (new_title, time.time(), item_id),
            )
            self._index_chunks(item_id, new_title, hist[1])
            self._con.commit()
        return True

    def dedupe(self, *, dry_run: bool = False) -> dict:
        """S5 知识去重: 跨 workspace 的重复条目 (同 title + 首 chunk 内容完全
        一致) — 知识库多工作区扫描常产生同源副本 (G5: 全局检索+副本清理)。

        保守规则: 仅当两条记录 title 相同 AND 内容指纹 (首个 chunk 内容)
        完全一致时视为重复; 保留 id 较小的一条, 其余 retired (不物理删除,
        保审计)。不同 workspace 的同内容条目也会去重 (跨工作区副本)。
        返回 {"retired": [ids], "dry_run": bool}。
        """
        with self._lock:
            rows = self._con.execute(
                "SELECT id, title FROM knowledge_items WHERE retired=0 ORDER BY id"
            ).fetchall()
            # 首 chunk 内容 (内容指纹)
            first_chunk: dict[int, str] = {}
            for rid, _t in rows:
                c = self._con.execute(
                    "SELECT content FROM knowledge_chunks WHERE item_id=? "
                    "ORDER BY chunk_index LIMIT 1",
                    (rid,),
                ).fetchone()
                first_chunk[rid] = c[0] if c else ""
        retired: list[int] = []
        seen: dict[tuple, int] = {}
        for rid, title in rows:
            key = ((title or "").strip(), first_chunk.get(rid, "").strip())
            if not key[0] or not key[1]:
                continue  # 无内容的不参与去重
            if key in seen:
                retired.append(rid)
                if not dry_run:
                    self._con.execute(
                        "UPDATE knowledge_items SET retired=1 WHERE id=?", (rid,)
                    )
            else:
                seen[key] = rid
        if not dry_run:
            self._con.commit()
        return {"retired": retired, "dry_run": dry_run}

    def close(self) -> None:
        with self._lock:
            try:
                self._con.close()
            except sqlite3.Error:
                pass
