"""Local skill-market statistics — install counts + ratings (SQLite).

The skill catalog itself lives on disk (SKILL.md folders); this store keeps the
marketplace numbers that don't belong in the skill file: install_count, ratings,
last installed timestamp. Keys are skill names.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class SkillMarketStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.execute(
            """CREATE TABLE IF NOT EXISTS skill_meta (
                name TEXT PRIMARY KEY,
                install_count INTEGER NOT NULL DEFAULT 0,
                rating_sum REAL NOT NULL DEFAULT 0,
                rating_count INTEGER NOT NULL DEFAULT 0,
                last_installed_at REAL
            )"""
        )
        self._con.commit()

    # -- installs ----------------------------------------------------------
    def record_install(self, name: str) -> None:
        with self._lock:
            self._con.execute(
                """INSERT INTO skill_meta (name, install_count, last_installed_at)
                   VALUES (?, 1, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     install_count = install_count + 1,
                     last_installed_at = ?""",
                (name, time.time(), time.time()),
            )
            self._con.commit()

    # -- ratings -----------------------------------------------------------
    def rate(self, name: str, score: float) -> dict:
        """Add a 1-5 rating; returns the new aggregate stats for the skill."""
        score = max(1.0, min(5.0, float(score)))
        with self._lock:
            self._con.execute(
                """INSERT INTO skill_meta (name, rating_sum, rating_count)
                   VALUES (?, ?, 1)
                   ON CONFLICT(name) DO UPDATE SET
                     rating_sum = rating_sum + ?, rating_count = rating_count + 1""",
                (name, score, score),
            )
            self._con.commit()
            row = self._con.execute(
                "SELECT install_count, rating_sum, rating_count FROM skill_meta WHERE name=?",
                (name,),
            ).fetchone()
        return self._aggregate(row)

    def stats(self, name: str) -> dict:
        with self._lock:
            row = self._con.execute(
                "SELECT install_count, rating_sum, rating_count FROM skill_meta WHERE name=?",
                (name,),
            ).fetchone()
        return self._aggregate(row)

    def all_stats(self) -> dict[str, dict]:
        with self._lock:
            rows = self._con.execute(
                "SELECT name, install_count, rating_sum, rating_count FROM skill_meta"
            ).fetchall()
        return {r[0]: self._aggregate(r[1:]) for r in rows}

    @staticmethod
    def _aggregate(row) -> dict:
        if row is None:
            return {
                "install_count": 0,
                "rating": None,
                "rating_count": 0,
            }
        install_count, rating_sum, rating_count = row
        return {
            "install_count": install_count,
            "rating": round(rating_sum / rating_count, 2) if rating_count else None,
            "rating_count": rating_count,
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._con.close()
            except sqlite3.Error:
                pass
