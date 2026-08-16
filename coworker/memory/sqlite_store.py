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
        # P2 MemCube 式元数据 (MemOS MemCube): version 版本链 / ttl 过期 /
        # origin 来源 / hotness 热度。全部幂等迁移 (列已存在则跳过)。
        for col, ddl in (
            ("use_count", "INTEGER NOT NULL DEFAULT 0"),
            ("stale", "INTEGER NOT NULL DEFAULT 0"),
            ("last_used_at", "TEXT"),
            ("version", "INTEGER NOT NULL DEFAULT 1"),
            ("ttl", "TEXT"),
            ("origin", "TEXT"),
            ("hotness", "REAL NOT NULL DEFAULT 0.0"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass  # column already present (fresh or migrated DB)
        # P2 版本链 (MemOS MemCube version chain): 每次 update 把旧内容快照进
        # memory_history, 支持审计与回滚 (rollback)。
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memhist ON memory_history(memory_id, version)"
        )
        self._conn.commit()

    def add(
        self,
        content: str,
        *,
        scope: Scope = Scope.WORKSPACE,
        key: Optional[str] = None,
        workspace: Optional[str] = None,
        session_id: Optional[str] = None,
        origin: Optional[str] = None,
        ttl: Optional[str] = None,
    ) -> MemoryItem:
        scope = Scope(scope)
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO memories (scope, key, content, workspace, session_id, origin, ttl) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scope.value, key, content, workspace, session_id, origin, ttl),
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
        """更新记忆内容, 同时把旧内容快照进版本链 (version + 1)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT content, version FROM memories WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            old_content = row["content"]
            old_version = int(row["version"] or 1)
            self._conn.execute(
                "INSERT INTO memory_history (memory_id, version, content) "
                "VALUES (?, ?, ?)",
                (item_id, old_version, old_content),
            )
            self._conn.execute(
                "UPDATE memories SET content = ?, version = ?, "
                "last_used_at = CURRENT_TIMESTAMP WHERE id = ?",
                (content, old_version + 1, item_id),
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
        """记忆被命中时刷新 use_count 与 last_used_at, 并重算热度 hotness
        (FADEMEM 频率信号 + MemCube hotness)。hotness = 1 - 1/(1+use_count):
        首次命中 0.5, 频繁命中单调趋近 1。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT use_count FROM memories WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return
            new_count = int(row["use_count"] or 0) + 1
            hotness = 1.0 - 1.0 / (1.0 + new_count)
            self._conn.execute(
                "UPDATE memories SET use_count = ?, last_used_at = CURRENT_TIMESTAMP, "
                "hotness = ? WHERE id = ?",
                (new_count, round(hotness, 4), item_id),
            )
            self._conn.commit()

    # -- P2 MemCube 式元数据 (MemOS MemCube) ----------------------------------
    def set_ttl(self, item_id: int, ttl: Optional[str]) -> bool:
        """设置/清除记忆的 TTL (ISO 8601 UTC 时间字符串; None = 永不过期)。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE memories SET ttl = ? WHERE id = ?", (ttl, item_id)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def history(self, item_id: int) -> list[dict[str, Any]]:
        """版本链: 历史版本 (旧->新) + 当前版本。用于审计与回滚。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, content, created_at FROM memory_history "
                "WHERE memory_id = ? ORDER BY version",
                (item_id,),
            ).fetchall()
            cur = self._conn.execute(
                "SELECT content, created_at FROM memories WHERE id = ?", (item_id,)
            ).fetchone()
        out = [dict(r) for r in rows]
        if cur is not None:
            cur_version = (rows[-1]["version"] + 1) if rows else 1
            out.append(
                {
                    "version": cur_version,
                    "content": cur["content"],
                    "created_at": cur["created_at"],
                    "current": True,
                }
            )
        return out

    def rollback(self, item_id: int, version: int) -> Optional[MemoryItem]:
        """回滚到指定版本: 恢复该版本内容并快照当前内容, version 链 +1。
        返回回滚后的记忆; 目标版本不存在时返回 None (不改动)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT version, content FROM memories WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            hist = self._conn.execute(
                "SELECT version, content FROM memory_history "
                "WHERE memory_id = ? AND version = ?",
                (item_id, version),
            ).fetchone()
            if hist is None and version != 1:
                return None
            target = hist["content"] if hist is not None else row["content"]
            if target == row["content"]:
                return self.get(item_id)  # 已在该版本, 无操作
            self._conn.execute(
                "INSERT INTO memory_history (memory_id, version, content) "
                "VALUES (?, ?, ?)",
                (item_id, row["version"], row["content"]),
            )
            self._conn.execute(
                "UPDATE memories SET content = ?, version = ?, "
                "last_used_at = CURRENT_TIMESTAMP WHERE id = ?",
                (target, row["version"] + 1, item_id),
            )
            self._conn.commit()
        return self.get(item_id)

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
    try:
        version = int(row["version"] or 1)
    except (KeyError, IndexError, TypeError, ValueError):
        version = 1
    try:
        hotness = float(row["hotness"] or 0.0)
    except (KeyError, IndexError, TypeError, ValueError):
        hotness = 0.0
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
        version=version,
        ttl=row["ttl"] if "ttl" in row.keys() else None,
        origin=row["origin"] if "origin" in row.keys() else None,
        hotness=hotness,
    )
