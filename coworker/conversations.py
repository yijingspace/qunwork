"""ConversationStore — global, file-backed session storage shared by all surfaces.

Layout under a base dir (default `~/.config/coworker/`):
  coworker.db                  SQLite index: sessions(id → project, title, n_msgs), workspaces, memory
  conversations/<id>.jsonl     append-only message log, one file per conversation

Writes append only the new messages each turn (no rewriting history). Legacy rows that
stored messages inline are lazily migrated to a .jsonl on first load/save.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .sessions import SessionRecord


def _load_roots(raw: Optional[str]) -> list[dict]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _load_grants(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _display_title(row: sqlite3.Row) -> Optional[str]:
    """Title precedence for every read path: a manual rename (renamed=1) always wins,
    then the generated auto_title, then the first-line snapshot `save()` wrote."""
    if row["renamed"]:
        return row["title"]
    return row["auto_title"] or row["title"]


def title_from(messages: list[dict]) -> str:
    from .attachments import content_to_text

    for m in messages:
        if m.get("role") == "user":
            text = content_to_text(m.get("content"), image_placeholder="").strip()
            if text:
                return text.splitlines()[0][:60]
    return "New task"


class ConversationStore:
    def __init__(self, base_dir: str | Path) -> None:
        self.base = Path(base_dir).expanduser()
        self.base.mkdir(parents=True, exist_ok=True)
        self.conv_dir = self.base / "conversations"
        self.conv_dir.mkdir(exist_ok=True)
        self.db_path = self.base / "coworker.db"

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, workspace TEXT, model TEXT, mode TEXT,
                title TEXT, agent TEXT DEFAULT 'code', n_msgs INTEGER DEFAULT 0, messages TEXT,
                extra_roots TEXT, pinned INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
                origin TEXT, origin_label TEXT,
                auto_title TEXT, renamed INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS workspaces (
                path TEXT PRIMARY KEY, last_used TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS task_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS swarm_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                intent TEXT NOT NULL,
                plan TEXT NOT NULL DEFAULT '[]',
                runs_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """)
        try:
            self._conn.execute("ALTER TABLE swarm_templates ADD COLUMN runs_count INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("ALTER TABLE swarm_templates ADD COLUMN success_count INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # columns already present
        for ddl in (
            "ALTER TABLE sessions ADD COLUMN title TEXT",
            "ALTER TABLE sessions ADD COLUMN n_msgs INTEGER DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN agent TEXT DEFAULT 'code'",
            "ALTER TABLE sessions ADD COLUMN extra_roots TEXT",
            "ALTER TABLE sessions ADD COLUMN pinned INTEGER DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN archived INTEGER DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN origin TEXT",
            "ALTER TABLE sessions ADD COLUMN origin_label TEXT",
            "ALTER TABLE sessions ADD COLUMN auto_title TEXT",
            "ALTER TABLE sessions ADD COLUMN renamed INTEGER DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN grants TEXT",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        self._conn.commit()
        self._backfill_counts()

    # -- file helpers -----------------------------------------------------------
    def _file(self, sid: str) -> Path:
        return self.conv_dir / f"{sid}.jsonl"

    def _read_jsonl(self, sid: str) -> Optional[list[dict]]:
        path = self._file(sid)
        if not path.exists():
            return None
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _count(self, sid: str) -> int:
        path = self._file(sid)
        if not path.exists():
            return 0
        return sum(
            1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        )

    def _append(self, sid: str, messages: list[dict]) -> None:
        with open(self._file(sid), "a", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps(m) + "\n")

    def _backfill_counts(self) -> None:
        """One-time per session: move any inline blob into a .jsonl and persist
        title + n_msgs in the index. Skips already-migrated rows on later startups."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, messages, n_msgs, title FROM sessions"
            ).fetchall()
            for row in rows:
                sid = row["session_id"]
                jsonl = self._file(sid)
                if jsonl.exists() and row["title"] and row["n_msgs"]:
                    continue  # already migrated
                if jsonl.exists():
                    messages = self._read_jsonl(sid) or []
                elif row["messages"]:
                    try:
                        messages = json.loads(row["messages"])
                    except json.JSONDecodeError:
                        messages = []
                    if messages:
                        self._append(sid, messages)
                    self._conn.execute(
                        "UPDATE sessions SET messages = NULL WHERE session_id = ?",
                        (sid,),
                    )
                else:
                    messages = []
                self._conn.execute(
                    "UPDATE sessions SET n_msgs = ?, title = ? WHERE session_id = ?",
                    (len(messages), row["title"] or title_from(messages), sid),
                )
            self._conn.commit()

    # -- API --------------------------------------------------------------------
    def list_swarm_templates(self) -> list[dict[str, Any]]:
        """Saved swarm runs (title + goal intent + task plan JSON) for one-click reuse."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, intent, plan, runs_count, success_count, created_at FROM swarm_templates ORDER BY id DESC"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["plan"] = json.loads(d.get("plan") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["plan"] = []
            d["runs_count"] = int(d.get("runs_count") or 0)
            d["success_count"] = int(d.get("success_count") or 0)
            out.append(d)
        return out

    def record_swarm_template_run(self, template_id: int, success: bool) -> bool:
        """Tally one reuse of a template: always increments runs_count, and bumps
        success_count when the run completed (strategy report 5.2.1 — a template
        carries its track record, not just its plan)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE swarm_templates SET runs_count = runs_count + 1, "
                "success_count = success_count + ? WHERE id = ?",
                (1 if success else 0, template_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def add_swarm_template(self, title: str, intent: str, plan: list | str | None) -> dict[str, Any]:
        title = (title or "").strip()
        intent = (intent or "").strip()
        if not title or not intent:
            raise ValueError("title and intent are required")
        plan_json = json.dumps(plan or [], ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO swarm_templates (title, intent, plan) VALUES (?, ?, ?)",
                (title, intent, plan_json),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id, title, intent, plan, runs_count, success_count, created_at FROM swarm_templates WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        d = dict(row)
        try:
            d["plan"] = json.loads(d.get("plan") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["plan"] = []
        d["runs_count"] = int(d.get("runs_count") or 0)
        d["success_count"] = int(d.get("success_count") or 0)
        return d

    def delete_swarm_template(self, template_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM swarm_templates WHERE id = ?", (template_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_task_templates(self) -> list[dict[str, Any]]:
        """User-defined home-task templates: title + the prompt that gets prefilled."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, prompt, created_at FROM task_templates ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def add_task_template(self, title: str, prompt: str) -> dict[str, Any]:
        title = (title or "").strip()
        prompt = (prompt or "").strip()
        if not title or not prompt:
            raise ValueError("title and prompt are required")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO task_templates (title, prompt) VALUES (?, ?)", (title, prompt)
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id, title, prompt, created_at FROM task_templates WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        return dict(row)

    def delete_task_template(self, template_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM task_templates WHERE id = ?", (template_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def save(self, record: SessionRecord) -> None:
        sid = record.session_id
        with self._lock:
            # lazily migrate a legacy inline blob into the .jsonl
            if not self._file(sid).exists():
                row = self._conn.execute(
                    "SELECT messages FROM sessions WHERE session_id = ?", (sid,)
                ).fetchone()
                if row and row["messages"]:
                    try:
                        legacy = json.loads(row["messages"])
                    except json.JSONDecodeError:
                        legacy = []
                    if legacy:
                        self._append(sid, legacy)

            # 增量基准 = 完整历史 (归档 + 活跃): 归档过的会话里, 活跃 .jsonl
            # 计数比完整历史小, 若按活跃计数追加会把归档消息重复写回。
            existing = len(self.full_history(sid))
            if len(record.messages) > existing:
                self._append(sid, record.messages[existing:])
            elif len(record.messages) < existing:  # rare; not append-only
                with open(self._file(sid), "w", encoding="utf-8") as f:
                    for m in record.messages:
                        f.write(json.dumps(m) + "\n")

            title = record.title or title_from(record.messages)
            self._conn.execute(
                """
                INSERT INTO sessions (session_id, workspace, model, mode, title, agent, n_msgs, messages, extra_roots, grants, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO UPDATE SET
                    workspace = excluded.workspace, model = excluded.model, mode = excluded.mode,
                    title = COALESCE(sessions.title, excluded.title), agent = excluded.agent,
                    n_msgs = excluded.n_msgs, messages = NULL, extra_roots = excluded.extra_roots,
                    grants = excluded.grants, updated_at = CURRENT_TIMESTAMP
                """,
                (
                    sid,
                    record.workspace,
                    record.model,
                    record.mode,
                    title,
                    record.agent,
                    len(record.messages),
                    json.dumps(record.extra_roots or []),
                    json.dumps(record.grants or {}),
                ),
            )
            self._conn.commit()
        self.touch_workspace(record.workspace)

    # -- 7x24 长程任务 (突破三): 皮萨诺压缩归档 ----------------------------------
    # 长会话 (7×24 任务可产生数百万条消息) 的 .jsonl 无限增长 (B4)。归档策略:
    #   * `archive_compressed(session_id, keep_recent)`: 把最旧的 N-keep_recent 条
    #     消息用 PisanoMemoryCompressor 无损压缩写入 `conversations/<sid>.pisano`,
    #     并从活跃 .jsonl 截断 (只留最近 keep_recent 条);
    #   * `read_archived(session_id)`: 无损解压归档, 与活跃消息合并还原完整历史;
    #   * 归档在会话初始化时自动合并 (`load()` 已接), 查询历史时按需展开。

    def _archive_file(self, sid: str) -> Path:
        return self.conv_dir / f"{sid}.pisano"

    def archive_compressed(
        self, session_id: str, keep_recent: int = 200
    ) -> Optional[dict[str, Any]]:
        """把会话的旧消息压缩归档, 活跃 .jsonl 只保留最近 keep_recent 条。

        基于**完整历史** (已有归档 + 活跃) 压缩: 重复归档时旧归档内容并入
        新压缩包 (无损, 不丢已归档消息)。返回归档摘要或 None (无可归档内容)。
        """
        from .memory.compressor import PisanoMemoryCompressor

        messages = self.full_history(session_id)  # 归档 + 活跃
        if not messages or len(messages) <= keep_recent:
            return None
        old = messages[:-keep_recent]
        recent = messages[-keep_recent:]
        comp = PisanoMemoryCompressor()
        packed = comp.compress_messages(old)
        archive_path = self._archive_file(session_id)
        archive_path.write_text(
            json.dumps(packed, ensure_ascii=False, default=str), encoding="utf-8"
        )
        # 截断活跃 .jsonl (仅保留最近 keep_recent 条)。
        with open(self._file(session_id), "w", encoding="utf-8") as f:
            for m in recent:
                f.write(json.dumps(m) + "\n")
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET n_msgs = ? WHERE session_id = ?",
                (len(recent), session_id),
            )
            self._conn.commit()
        return {
            "archived": len(old),
            "recent": len(recent),
            "compression_ratio": packed.get("compression_ratio", 0.0),
            "archive_file": str(archive_path),
        }

    def read_archived(self, session_id: str) -> list[dict]:
        """无损解压归档消息 (旧历史)。无归档时返回空列表。"""
        from .memory.compressor import PisanoMemoryCompressor

        path = self._archive_file(session_id)
        if not path.exists():
            return []
        try:
            packed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return PisanoMemoryCompressor().decompress(packed)

    def full_history(self, session_id: str) -> list[dict]:
        """完整历史 = 归档 (旧) + 活跃 (新)。供历史回放/导出。"""
        return self.read_archived(session_id) + (self._read_jsonl(session_id) or [])

    def load(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        # 7x24 (突破三): 完整历史 = 皮萨诺归档 (旧) + 活跃 .jsonl (新)。
        messages = self.full_history(session_id)
        if not messages:
            try:
                messages = json.loads(row["messages"] or "[]")
            except json.JSONDecodeError:
                messages = []
        return SessionRecord(
            session_id=session_id,
            workspace=row["workspace"],
            model=row["model"],
            mode=row["mode"],
            messages=messages,
            title=_display_title(row),
            agent=row["agent"] or "code",
            message_count=len(messages),
            updated_at=row["updated_at"],
            extra_roots=_load_roots(
                row["extra_roots"] if "extra_roots" in row.keys() else None
            ),
            grants=_load_grants(row["grants"] if "grants" in row.keys() else None),
            pinned=bool(row["pinned"]),
            archived=bool(row["archived"]),
            origin=row["origin"],
            origin_label=row["origin_label"],
        )

    def set_extra_roots(self, session_id: str, extra_roots: list[dict]) -> None:
        """Persist just the session's added folders, independent of its message log — used when
        the user adds/removes a folder (which may happen with no active engine)."""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET extra_roots = ?, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                (json.dumps(extra_roots or []), session_id),
            )
            self._conn.commit()

    def list(self, *, workspace: Optional[str] = None) -> list[SessionRecord]:
        with self._lock:
            if workspace is None:
                rows = self._conn.execute(
                    "SELECT * FROM sessions ORDER BY pinned DESC, updated_at DESC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM sessions WHERE workspace = ? ORDER BY pinned DESC, updated_at DESC",
                    (workspace,),
                ).fetchall()
        return [
            SessionRecord(
                session_id=r["session_id"],
                workspace=r["workspace"],
                model=r["model"],
                mode=r["mode"],
                messages=[],
                title=_display_title(r),
                agent=r["agent"] or "code",
                message_count=r["n_msgs"] or 0,
                updated_at=r["updated_at"],
                pinned=bool(r["pinned"]),
                archived=bool(r["archived"]),
                origin=r["origin"],
                origin_label=r["origin_label"],
            )
            for r in rows
        ]

    def touch_workspace(self, path: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO workspaces (path, last_used) VALUES (?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(path) DO UPDATE SET last_used = CURRENT_TIMESTAMP",
                (path,),
            )
            self._conn.commit()

    def recent_workspaces(self, limit: int = 20) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM workspaces ORDER BY last_used DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r["path"] for r in rows]

    def canonicalize_workspaces(self) -> None:
        with self._lock:
            for (ws,) in self._conn.execute(
                "SELECT DISTINCT workspace FROM sessions WHERE workspace IS NOT NULL"
            ).fetchall():
                real = os.path.realpath(ws)
                if real != ws:
                    self._conn.execute(
                        "UPDATE sessions SET workspace = ? WHERE workspace = ?",
                        (real, ws),
                    )
            latest: dict[str, str] = {}
            for path, last in self._conn.execute(
                "SELECT path, last_used FROM workspaces"
            ).fetchall():
                real = os.path.realpath(path)
                if real not in latest or (last or "") > latest[real]:
                    latest[real] = last
            self._conn.execute("DELETE FROM workspaces")
            for path, last in latest.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO workspaces (path, last_used) VALUES (?, ?)",
                    (path, last),
                )
            self._conn.commit()

    def delete(self, session_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )
            self._conn.commit()
        path = self._file(session_id)
        if path.exists():
            path.unlink()
        return cur.rowcount > 0

    def rename(self, session_id: str, title: str) -> bool:
        clean = " ".join((title or "").split())[:120]
        if not clean:
            return False
        with self._lock:
            # renamed=1 makes the manual title final: auto-titling skips the session and
            # `_display_title` ignores any auto_title already there.
            cur = self._conn.execute(
                "UPDATE sessions SET title = ?, renamed = 1, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                (clean, session_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_auto_title(self, session_id: str, title: str) -> bool:
        """Store a generated title. Its own column — never `title` — so a manual rename
        (past or future) always wins; doesn't touch updated_at (a title landing after the
        turn must not reorder the session list)."""
        clean = " ".join((title or "").split())[:60]
        if not clean:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET auto_title = ? WHERE session_id = ? AND renamed = 0",
                (clean, session_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def title_state(self, session_id: str) -> Optional[dict]:
        """The auto-title guard inputs: whether the user renamed and whether a generated
        title already exists. None when the session has no row yet."""
        with self._lock:
            row = self._conn.execute(
                "SELECT renamed, auto_title FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {"renamed": bool(row["renamed"]), "auto_title": row["auto_title"]}

    def set_flags(
        self,
        session_id: str,
        *,
        pinned: Optional[bool] = None,
        archived: Optional[bool] = None,
    ) -> bool:
        """Update pin/archive flags without touching updated_at (so pinning doesn't reorder)."""
        sets, params = [], []
        if pinned is not None:
            sets.append("pinned = ?")
            params.append(1 if pinned else 0)
        if archived is not None:
            sets.append("archived = ?")
            params.append(1 if archived else 0)
        if not sets:
            return False
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE session_id = ?",
                (*params, session_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_origin(self, session_id: str, origin: str, origin_label: str = "") -> bool:
        """Mark where a spawned session came from (§31). Set once at spawn; `save()` never
        names these columns, so per-turn saves can't clobber them (the pinned mechanism).
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET origin = ?, origin_label = ? WHERE session_id = ?",
                (origin, origin_label or None, session_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()
