"""Orchestration run store — real-time progress + history (Phase 5).

SQLite persistence for every orchestration run and its event stream, so the
frontend can poll a run's live progress (plan → worker thoughts → verdicts →
governance commands) and revisit past runs.

Tables:
- orchestration_runs(run_id, intent, status, created_at, updated_at, final)
- orchestration_events(id, run_id, seq, kind, payload, ts)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class OrchestrationRunStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # The run store is written from background tasks / polled from request
        # threads; allow cross-thread use (guarded by the lock).
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS orchestration_runs (
                run_id TEXT PRIMARY KEY,
                intent TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                final TEXT
            )"""
        )
        # P0 建议3 (保留分支 A/B): a run may be a fork of a parent run — the parent
        # link lets the UI group the branches under their origin run.
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(orchestration_runs)")]
        if "parent_run_id" not in cols:
            self._db.execute(
                "ALTER TABLE orchestration_runs ADD COLUMN parent_run_id TEXT"
            )
        # ROI 价值标签 (建议10): 每次蜂群运行可打业务标签, 报告按标签分组。
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(orchestration_runs)")]
        if "value_tag" not in cols:
            self._db.execute(
                "ALTER TABLE orchestration_runs ADD COLUMN value_tag TEXT"
            )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS orchestration_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                ts REAL NOT NULL
            )"""
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_oe_run ON orchestration_events(run_id, seq)"
        )
        # 7x24 长程任务 (突破方案五): 降级记录 — 每次分形降级动作落一条,
        # 供审计与恢复 (哪些任务降过级、降了几级、保真度多少)。
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS orchestration_degradations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                level INTEGER NOT NULL,
                action TEXT NOT NULL,
                fidelity REAL NOT NULL,
                error TEXT,
                ts REAL NOT NULL
            )"""
        )
        self._db.commit()

    # -- runs ---------------------------------------------------------------
    def create_run(self, intent: str, *, parent_run_id: Optional[str] = None) -> str:
        run_id = f"orch_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO orchestration_runs (run_id, intent, status, created_at, updated_at, parent_run_id) VALUES (?,?,?,?,?,?)",
                (run_id, intent, "running", now, now, parent_run_id),
            )
            self._db.commit()
        return run_id

    def update_status(self, run_id: str, status: str, final: Optional[str] = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE orchestration_runs SET status = ?, updated_at = ?, final = COALESCE(?, final) WHERE run_id = ?",
                (status, time.time(), final, run_id),
            )
            self._db.commit()

    def set_value_tag(self, run_id: str, value_tag: str) -> bool:
        """ROI 价值标签 (建议10): tag a finished run with a business label."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE orchestration_runs SET value_tag = ? WHERE run_id = ?",
                (value_tag or None, run_id),
            )
            self._db.commit()
            return cur.rowcount > 0

    def reap_orphaned_runs(
        self, *, older_than: float = 120.0, active_run_ids: Optional[set[str]] = None
    ) -> int:
        """Flip runs the GUI still shows as 'running' but that are actually dead.

        A sidecar restart kills the in-process orchestration task, but the DB row
        keeps status='running' forever — SwarmView then spins a live run that will
        never finish (the tech-debt this closes). `append_event` bumps updated_at as
        a heartbeat, so a row silent for `older_than` seconds is orphaned. Run ids in
        `active_run_ids` (the manager's live controls) are always spared. Returns the
        count reaped. Mirrors automation's `reap_stale_runs` (status → 'failed')."""
        active = active_run_ids or set()
        cutoff = time.time() - older_than
        placeholders = ",".join("?" * len(active)) if active else ""
        q = "SELECT run_id FROM orchestration_runs WHERE status = 'running' AND updated_at < ?"
        params: list[Any] = [cutoff]
        if active:
            q += f" AND run_id NOT IN ({placeholders})"
            params += list(active)
        with self._lock:
            ids = [r[0] for r in self._db.execute(q, params).fetchall()]
            for rid in ids:
                self._db.execute(
                    "UPDATE orchestration_runs SET status = 'failed', updated_at = ?, "
                    "final = COALESCE(final, ?) WHERE run_id = ?",
                    (
                        time.time(),
                        "⚠ 服务重启导致该蜂群运行中断（无心跳，已回收）。可稍后重新发起。",
                        rid,
                    ),
                )
            self._db.commit()
        return len(ids)

    def prune_events(self, *, keep_runs: int = 100) -> int:
        """Delete event rows for all but the most recent `keep_runs` finished runs.

        Each run streams hundreds of worker_thought / decision_trace rows that are
        only useful while watching it; history keeps the run summary + `final`, so
        the raw stream can go. Currently-running runs and the most recent
        `keep_runs` by created_at are preserved. Returns the number of rows deleted.
        Best-effort VACUUMs to reclaim disk."""
        with self._lock:
            keep = self._db.execute(
                "SELECT run_id FROM orchestration_runs WHERE status = 'running' "
                "UNION "
                "SELECT run_id FROM ( "
                "  SELECT run_id FROM orchestration_runs "
                "  ORDER BY created_at DESC LIMIT ? "
                ")",
                (max(1, int(keep_runs)),),
            ).fetchall()
            keep_ids = {r[0] for r in keep}
            all_ids = {
                r[0]
                for r in self._db.execute(
                    "SELECT DISTINCT run_id FROM orchestration_events"
                ).fetchall()
            }
            doomed = all_ids - keep_ids
            deleted = 0
            if doomed:
                ph = ",".join("?" * len(doomed))
                cur = self._db.execute(
                    f"DELETE FROM orchestration_events WHERE run_id IN ({ph})",
                    list(doomed),
                )
                deleted = cur.rowcount or 0
                self._db.commit()
                try:
                    self._db.execute("VACUUM")  # reclaim disk after bulk delete
                except sqlite3.OperationalError:
                    pass  # VACUUM cannot run inside a transaction / is locked — skip
        return deleted

    # -- events -------------------------------------------------------------
    def append_event(self, run_id: str, kind: str, payload: dict[str, Any]) -> int:
        with self._lock:
            seq = self._next_seq(run_id)
            now = time.time()
            self._db.execute(
                "INSERT INTO orchestration_events (run_id, seq, kind, payload, ts) VALUES (?,?,?,?,?)",
                (run_id, seq, kind, json.dumps(payload, ensure_ascii=False), now),
            )
            # heartbeat: keep updated_at fresh while the run is alive so pollers can
            # tell a live run from an orphaned one (background task lost on restart).
            self._db.execute(
                "UPDATE orchestration_runs SET updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            self._db.commit()
        return seq

    def _next_seq(self, run_id: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM orchestration_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row[0]) + 1

    # -- degradations (7x24 突破五) ------------------------------------------
    def record_degradation(
        self,
        run_id: str,
        task_id: str,
        level: int,
        action: str,
        *,
        fidelity: float = 0.0,
        error: Optional[str] = None,
    ) -> int:
        """记录一次分形降级 (L1→L6), 返回记录 id。"""
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO orchestration_degradations "
                "(run_id, task_id, level, action, fidelity, error, ts) VALUES (?,?,?,?,?,?,?)",
                (run_id, task_id, int(level), action, float(fidelity), error, time.time()),
            )
            self._db.commit()
        return int(cur.lastrowid)

    def list_degradations(self, run_id: Optional[str] = None) -> list[dict[str, Any]]:
        """降级审计轨迹: 按 run 过滤或全部 (时间正序)。"""
        if run_id:
            rows = self._db.execute(
                "SELECT run_id, task_id, level, action, fidelity, error, ts "
                "FROM orchestration_degradations WHERE run_id = ? ORDER BY ts",
                (run_id,),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT run_id, task_id, level, action, fidelity, error, ts "
                "FROM orchestration_degradations ORDER BY ts",
            ).fetchall()
        return [
            {
                "run_id": r[0],
                "task_id": r[1],
                "level": r[2],
                "action": r[3],
                "fidelity": r[4],
                "error": r[5],
                "ts": r[6],
            }
            for r in rows
        ]

    # -- reads --------------------------------------------------------------
    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT run_id, intent, status, created_at, updated_at, final, parent_run_id, value_tag FROM orchestration_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if not row:
                return None
            events = self._db.execute(
                "SELECT kind, payload FROM orchestration_events WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
            degradations = self._db.execute(
                "SELECT task_id, level, action, fidelity, error, ts "
                "FROM orchestration_degradations WHERE run_id = ? ORDER BY ts",
                (run_id,),
            ).fetchall()
        return {
            "run_id": row[0],
            "intent": row[1],
            "status": row[2],
            "created_at": row[3],
            "updated_at": row[4],
            "final": row[5],
            "parent_run_id": row[6],
            "value_tag": row[7],
            "events": [
                {"kind": k, "payload": json.loads(p)} for k, p in events
            ],
            # 7x24 长程任务 (突破方案五): 降级轨迹 — 供 GUI 展示降级链。
            "degradations": [
                {
                    "task_id": d[0],
                    "level": d[1],
                    "action": d[2],
                    "fidelity": d[3],
                    "error": d[4],
                    "ts": d[5],
                }
                for d in degradations
            ],
        }

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT run_id, intent, status, created_at, updated_at, parent_run_id, value_tag FROM orchestration_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "run_id": r[0],
                "intent": r[1],
                "status": r[2],
                "created_at": r[3],
                "updated_at": r[4],
                "parent_run_id": r[5],
                "value_tag": r[6],
            }
            for r in rows
        ]

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            logger.debug("run store close failed", exc_info=True)


# -- read-only accessors (双 orchestration.db 只读聚合) -------------------------
# A per-workspace store lives at <workspace>/.qunwork/orchestration.db (the chat
# `orchestrate()` tool writes there); the GUI panel store is the global one. These
# let the history list surface BOTH without any writes — unlike OrchestrationRunStore
# (whose __init__ mkdirs + CREATE TABLE + ALTER), which would CLOBBER every scanned
# workspace by creating an empty DB. Here we open `mode=ro` URIs, never create a
# file, and swallow every error (missing file / missing table / locked) as "nothing".
_READ_COLS = (
    "run_id, intent, status, created_at, updated_at, parent_run_id, value_tag"
)


def _ro_connect(db_path: str | Path) -> Optional[sqlite3.Connection]:
    p = Path(db_path)
    if not p.is_file():
        return None  # mode=ro would NOT create it; skip cleanly
    try:
        # as_uri() → file:///E:/.../orchestration.db (Windows-safe, absolute);
        # append mode=ro so sqlite opens it read-only (no write lock, no create).
        return sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return None


def read_only_list_runs(db_path: str | Path, limit: int = 50) -> list[dict[str, Any]]:
    """List runs from another orchestration.db strictly read-only. Returns [] on any
    problem (absent file, no table yet, locked). Adds no writes to the target."""
    conn = _ro_connect(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            f"SELECT {_READ_COLS} FROM orchestration_runs "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return [
        {
            "run_id": r[0],
            "intent": r[1],
            "status": r[2],
            "created_at": r[3],
            "updated_at": r[4],
            "parent_run_id": r[5],
            "value_tag": r[6],
        }
        for r in rows
    ]


def read_only_get_run(db_path: str | Path, run_id: str) -> Optional[dict[str, Any]]:
    """Fetch one run (+ its event stream) from another orchestration.db, read-only."""
    conn = _ro_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            f"SELECT {_READ_COLS}, final FROM orchestration_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        try:
            events = conn.execute(
                "SELECT kind, payload FROM orchestration_events WHERE run_id = ? "
                "ORDER BY seq",
                (run_id,),
            ).fetchall()
        except sqlite3.Error:
            events = []
    except sqlite3.Error:
        return None
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return {
        "run_id": row[0],
        "intent": row[1],
        "status": row[2],
        "created_at": row[3],
        "updated_at": row[4],
        "parent_run_id": row[5],
        "value_tag": row[6],
        "final": row[7],
        "events": [{"kind": k, "payload": json.loads(p)} for k, p in events],
    }
