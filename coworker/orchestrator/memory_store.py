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
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .vectormemory import Embedder, MemoryHit, VectorMemory


class PersistentVectorMemory:
    """SQLite-backed vector memory with scope isolation."""

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
        self._db = sqlite3.connect(str(self._path))
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
        self._db.commit()
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self) -> None:
        rows = self._db.execute(
            "SELECT text, meta, vector FROM vector_memories WHERE scope = ?",
            (self.scope,),
        ).fetchall()
        for text, meta, vec in rows:
            item = self._mem._new_item(text, json.loads(meta))
            if vec:
                try:
                    item.vector = json.loads(vec)
                except json.JSONDecodeError:
                    item.vector = None
            self._mem.items.append(item)

    def add(self, text: str, **meta: Any) -> None:
        item = self._mem._new_item(text, meta)
        if item.vector is None and self._mem.embedder is not None:
            try:
                item.vector = self._mem.embedder(text)
            except Exception:
                pass
        self._mem.items.append(item)
        self._db.execute(
            "INSERT INTO vector_memories (scope, text, meta, vector, created_at) VALUES (?,?,?,?,?)",
            (
                self.scope,
                text,
                json.dumps(meta, ensure_ascii=False),
                json.dumps(item.vector) if item.vector is not None else None,
                time.time(),
            ),
        )
        self._db.commit()

    def search(self, query: str, k: int = 3) -> list[MemoryHit]:
        return self._mem.search(query, k=k)

    def __len__(self) -> int:
        return len(self._mem)

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass
