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
