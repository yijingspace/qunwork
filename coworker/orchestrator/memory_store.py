"""Persistent vector memory — cross-session, cross-task episodic memory (Phase 4).

VectorMemory in Phase 3 lives in process and dies with the run. This store
persists memory items to SQLite, scoped by a `scope` string (e.g. the workspace
path or a project id), so a later orchestration run — even in a fresh process —
can retrieve lessons/results from earlier runs.

Design: one SQLite table, vector serialized as JSON text (fine for MVP scale;
swap for a real vector DB when the corpus grows). Items are loaded into the
in-memory VectorMemory on open; adds are written through immediately.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .vectormemory import Embedder, MemoryHit, VectorMemory

logger = logging.getLogger(__name__)


class PersistentVectorMemory:
    """SQLite-backed vector memory with scope isolation.

    Owner-audit 2026-08-07 (bug #7): sqlite3 connections are thread-bound by
    default. ``run_orchestration`` may be invoked from a worker thread while
    the same store is read from the main thread (or vice-versa via the asset
    interconnect). We open with ``check_same_thread=False`` and guard every
    DB access with a lock, matching the pattern in ``run_store.py``.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        scope: str,
        embedder: Optional[Embedder] = None,
    ) -> None:
        self.scope = scope
        self._mem = VectorMemory(embedder=embedder)
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS vector_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                text TEXT NOT NULL,
                meta TEXT NOT NULL,
                vector TEXT,
                created_at REAL NOT NULL
            )"""
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_vm_scope ON vector_memories(scope)"
        )
        # T5 phase index: periodic tasks tag memories with a phase slot (e.g. the
        # Pisano-period slot of the task's ordinal), so re-runs of the same kind of
        # task reuse same-phase history instead of re-reading everything.
        try:
            self._db.execute("ALTER TABLE vector_memories ADD COLUMN phase INTEGER")
            self._db.commit()
        except sqlite3.OperationalError:
            pass  # column already present
        # P1 记忆维护 (GuaAgent/OpenClaw 文档): use_count 访问频率 + last_used_at
        # 最后命中时间 — FADEMEM 衰减公式的输入; 幂等迁移 (列已存在则跳过)。
        for col, ddl in (
            ("use_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_used_at", "REAL"),
        ):
            try:
                self._db.execute(
                    f"ALTER TABLE vector_memories ADD COLUMN {col} {ddl}"
                )
                self._db.commit()
            except sqlite3.OperationalError:
                pass  # column already present
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self) -> None:
        with self._lock:
            rows = self._db.execute(
                "SELECT text, meta, vector FROM vector_memories WHERE scope = ?",
                (self.scope,),
            ).fetchall()
        for text, meta, vec in rows:
            # P1 衰减遗忘: stale 条目 (meta.stale=true) 不载入内存 → 检索自动
            # 排除; 数据保留在库中 (Manus 降级而非抹除), 可手动恢复。
            try:
                meta_obj = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta_obj = {}
            if meta_obj.get("stale"):
                continue
            item = self._mem._new_item(text, meta_obj)
            if vec:
                try:
                    item.vector = json.loads(vec)
                except json.JSONDecodeError:
                    item.vector = None
            self._mem.items.append(item)

    def add(
        self, text: str, *, phase: Optional[int] = None, **meta: Any
    ) -> None:
        if phase is not None:
            meta["phase"] = int(phase)
        item = self._mem._new_item(text, meta)
        if item.vector is None and self._mem.embedder is not None:
            try:
                item.vector = self._mem.embedder(text)
            except Exception:
                logger.debug("persistent memory embedder failed", exc_info=True)
        # C12: _mem.items is shared mutable state; append and DB insert under the
        # SAME lock so a concurrent search() never iterates a half-appended list.
        with self._lock:
            self._mem.items.append(item)
            self._db.execute(
                "INSERT INTO vector_memories (scope, text, meta, vector, created_at, phase) VALUES (?,?,?,?,?,?)",
                (
                    self.scope,
                    text,
                    json.dumps(meta, ensure_ascii=False),
                    json.dumps(item.vector) if item.vector is not None else None,
                    time.time(),
                    int(phase) if phase is not None else None,
                ),
            )
            self._db.commit()

    def search(
        self, query: str, k: int = 3, phase: Optional[int] = None
    ) -> list[MemoryHit]:
        """Top-k memory hits. With `phase`, same-phase history is preferred and
        the rest of k is backfilled from the global store (T5 periodic reuse)."""
        # C12: search reads the shared in-memory items list — hold the lock so
        # a concurrent add() never mutates it mid-iteration.
        with self._lock:
            hits = self._mem.search(query, k=k)
            # P1 衰减遗忘 (FADEMEM): 命中的记忆刷新 use_count — 高频使用的
            # 记忆在衰减 pass 中获得新鲜度地板, 不会被误标 stale。
            if hits:
                text_ids = {h.text: None for h in hits}
                placeholders = ",".join("?" * len(text_ids))
                self._db.execute(
                    f"UPDATE vector_memories SET use_count = use_count + 1, "
                    f"last_used_at = ? WHERE scope = ? AND text IN ({placeholders})",
                    [time.time(), self.scope, *text_ids.keys()],
                )
                self._db.commit()
        if phase is None:
            return hits
        with self._lock:
            rows = self._db.execute(
                "SELECT text, meta, vector FROM vector_memories WHERE scope = ? AND phase = ?",
                (self.scope, int(phase)),
            ).fetchall()
        if not rows:
            return hits
        temp = VectorMemory(embedder=self._mem.embedder)
        for text, meta, vec in rows:
            item = temp._new_item(text, json.loads(meta))
            if vec:
                try:
                    item.vector = json.loads(vec)
                except json.JSONDecodeError:
                    item.vector = None
            temp.items.append(item)
        phase_hits = temp.search(query, k=k)
        # merge: same-phase hits first (dedup by text), backfill with global hits
        seen: set[str] = set()
        merged: list[MemoryHit] = []
        for h in phase_hits + hits:
            if h.text in seen:
                continue
            seen.add(h.text)
            merged.append(h)
            if len(merged) >= k:
                break
        return merged

    def __len__(self) -> int:
        return len(self._mem)

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:
                logger.debug("memory store close failed", exc_info=True)
