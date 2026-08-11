# -*- coding: utf-8 -*-
"""Token usage ledger for QunWork.

Every model turn that reports usage (OpenAI-compatible `usage` trailer) is
recorded here by the engine's usage_sink. The aggregates feed the UI: per-day /
per-session totals and — the point of this feature — the context-cache hit rate
(cached_tokens / prompt_tokens), so you can see how much of every request is
being served from DeepSeek's (or any provider's) automatic prefix cache.

Schema is deliberately append-only and tiny; retention is not enforced here.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


def _now() -> float:
    return time.time()


class UsageStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._con = sqlite3.connect(self.path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._con.execute(
                """
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    surface TEXT,
                    run_id TEXT,
                    model TEXT,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
            self._con.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_session ON token_usage(session_id)"
            )
            self._con.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_created ON token_usage(created_at)"
            )
            self._con.commit()

    def record(self, entry: dict[str, Any]) -> None:
        """Persist one turn's usage (engine usage_sink payload)."""
        try:
            with self._lock:
                self._con.execute(
                    "INSERT INTO token_usage (session_id, surface, run_id, model, "
                    "prompt_tokens, completion_tokens, cached_tokens, cache_miss_tokens, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        entry.get("session_id"),
                        entry.get("surface"),
                        entry.get("run_id"),
                        entry.get("model"),
                        int(entry.get("prompt_tokens") or 0),
                        int(entry.get("completion_tokens") or 0),
                        int(entry.get("cached_tokens") or 0),
                        int(entry.get("cache_miss_tokens") or 0),
                        entry.get("ts") or _now(),
                    ),
                )
                self._con.commit()
        except Exception:
            pass  # never let accounting break the turn

    # -- aggregates ----------------------------------------------------------

    @staticmethod
    def _hit_rate(cached: int, prompt: int) -> float:
        return round(cached / prompt, 4) if prompt > 0 else 0.0

    def totals(self) -> dict[str, Any]:
        with self._lock:
            row = self._con.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c, "
                "COALESCE(SUM(cached_tokens),0) h, COUNT(*) n FROM token_usage"
            ).fetchone()
        p, c, h, n = row["p"], row["c"], row["h"], row["n"]
        return {
            "prompt_tokens": int(p),
            "completion_tokens": int(c),
            "total_tokens": int(p + c),
            "cached_tokens": int(h),
            "cache_hit_rate": self._hit_rate(h, p),
            "turns": int(n),
        }

    def surface_totals(self, surface: str, since: float) -> dict[str, Any]:
        """Token totals for one `surface` (e.g. 'cachewarm') since `since` —
        used to report warm-up spend without mixing it into user totals."""
        with self._lock:
            row = self._con.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0) p, "
                "COALESCE(SUM(cached_tokens),0) h, COUNT(*) n "
                "FROM token_usage WHERE surface=? AND created_at >= ?",
                (surface, since),
            ).fetchone()
        return {
            "prompt_tokens": int(row["p"]),
            "cached_tokens": int(row["h"]),
            "calls": int(row["n"]),
        }

    def by_day(self, days: int = 14) -> list[dict[str, Any]]:
        """Per-day totals for the last `days` days (oldest first)."""
        since = _now() - days * 86400
        with self._lock:
            rows = self._con.execute(
                "SELECT strftime('%Y-%m-%d', created_at, 'unixepoch', 'localtime') day, "
                "SUM(prompt_tokens) p, SUM(completion_tokens) c, SUM(cached_tokens) h "
                "FROM token_usage WHERE created_at >= ? GROUP BY day ORDER BY day",
                (since,),
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "day": r["day"],
                    "prompt_tokens": int(r["p"] or 0),
                    "completion_tokens": int(r["c"] or 0),
                    "cached_tokens": int(r["h"] or 0),
                    "cache_hit_rate": self._hit_rate(int(r["h"] or 0), int(r["p"] or 0)),
                }
            )
        return out

    def by_session(self, limit: int = 20) -> list[dict[str, Any]]:
        """Per-session totals, most active first."""
        with self._lock:
            rows = self._con.execute(
                "SELECT session_id, model, COUNT(*) turns, "
                "SUM(prompt_tokens) p, SUM(completion_tokens) c, SUM(cached_tokens) h, "
                "MAX(created_at) last "
                "FROM token_usage WHERE session_id IS NOT NULL GROUP BY session_id "
                "ORDER BY last DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "session_id": r["session_id"],
                    "model": r["model"],
                    "turns": int(r["turns"]),
                    "prompt_tokens": int(r["p"] or 0),
                    "completion_tokens": int(r["c"] or 0),
                    "cached_tokens": int(r["h"] or 0),
                    "cache_hit_rate": self._hit_rate(int(r["h"] or 0), int(r["p"] or 0)),
                }
            )
        return out

    def session_total(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._con.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c, "
                "COALESCE(SUM(cached_tokens),0) h, COUNT(*) n FROM token_usage WHERE session_id=?",
                (session_id,),
            ).fetchone()
        p, c, h = row["p"], row["c"], row["h"]
        return {
            "session_id": session_id,
            "prompt_tokens": int(p),
            "completion_tokens": int(c),
            "total_tokens": int(p + c),
            "cached_tokens": int(h),
            "cache_hit_rate": self._hit_rate(h, p),
            "turns": int(row["n"]),
        }


def render_usage_summary(u: dict[str, Any]) -> str:
    """Compact human-readable one-liner used by CLI/notices."""
    return (
        f"{u.get('prompt_tokens', 0):,} in / {u.get('completion_tokens', 0):,} out "
        f"(cache hit {u.get('cache_hit_rate', 0) * 100:.0f}%)"
    )
