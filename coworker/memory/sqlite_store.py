"""SQLite-backed memory store (the default adapter)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .base import MemoryItem, MemoryStore, Scope


class SQLiteMemoryStore(MemoryStore):
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the server runs the WS handler on a different thread
        # than the store was created on; a lock serializes access.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                key TEXT,
                content TEXT NOT NULL,
                workspace TEXT,
                session_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """)
        # P1 记忆维护 (GuaAgent/OpenClaw 文档): use_count 记录访问频率,
        # stale 标记衰减后的冷记忆 (从注入隐藏但保留在库中, 可恢复)。
        for col, ddl in (
            ("use_count", "INTEGER NOT NULL DEFAULT 0"),
            ("stale", "INTEGER NOT NULL DEFAULT 0"),
            ("last_used_at", "TEXT"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass  # column already present (fresh or migrated DB)
        self._conn.commit()

    def add(
        self,
        content: str,
        *,
        scope: Scope = Scope.WORKSPACE,
        key: Optional[str] = None,
        workspace: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> MemoryItem:
        scope = Scope(scope)
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO memories (scope, key, content, workspace, session_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope.value, key, content, workspace, session_id),
            )
            self._conn.commit()
            item = self.get(cursor.lastrowid)
        assert item is not None
        return item

    def get(self, item_id: int) -> Optional[MemoryItem]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (item_id,)
            ).fetchone()
        return _row_to_item(row) if row else None

    def list(
        self,
        *,
        scope: Optional[Scope] = None,
        workspace: Optional[str] = None,
        session_id: Optional[str] = None,
        include_stale: bool = False,
    ) -> list[MemoryItem]:
        query = "SELECT * FROM memories WHERE 1 = 1"
        params: list[object] = []
        if scope is not None:
            query += " AND scope = ?"
            params.append(Scope(scope).value)
        if workspace is not None:
            query += " AND workspace = ?"
            params.append(workspace)
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        if not include_stale:
            # P1: 冷记忆默认隐藏 (衰减遗忘); 维护/审计场景传 include_stale=True。
            query += " AND COALESCE(stale, 0) = 0"
        query += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_item(row) for row in rows]

    def update(self, item_id: int, content: str) -> Optional[MemoryItem]:
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET content = ? WHERE id = ?", (content, item_id)
            )
            self._conn.commit()
        return self.get(item_id)

    def delete(self, item_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM memories WHERE id = ?", (item_id,))
            self._conn.commit()
        return cursor.rowcount > 0

    # -- P1 衰减遗忘 (GuaAgent/OpenClaw 文档) ---------------------------------
    def mark_stale(self, item_id: int, stale: bool = True) -> bool:
        """标记/清除冷记忆的 stale 状态 — 只隐藏不删除, 可恢复 (Manus 降级)。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE memories SET stale = ? WHERE id = ?", (1 if stale else 0, item_id)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def bump_usage(self, item_id: int) -> None:
        """记忆被命中时刷新 use_count 与 last_used_at (FADEMEM 频率信号)。"""
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET use_count = use_count + 1, "
                "last_used_at = CURRENT_TIMESTAMP WHERE id = ?",
                (item_id,),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _row_to_item(row: sqlite3.Row) -> MemoryItem:
    try:
        use_count = int(row["use_count"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        use_count = 0
    try:
        stale = bool(row["stale"])
    except (KeyError, IndexError, TypeError, ValueError):
        stale = False
    return MemoryItem(
        id=row["id"],
        scope=Scope(row["scope"]),
        content=row["content"],
        key=row["key"],
        workspace=row["workspace"],
        session_id=row["session_id"],
        created_at=row["created_at"],
        use_count=use_count,
        stale=stale,
    )
