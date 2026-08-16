"""Local skill-market statistics — install counts + ratings per release (SQLite).

The skill catalog itself lives on disk (SKILL.md folders); this store keeps the
marketplace numbers that don't belong in the skill file: install_count, ratings,
last installed timestamp.

Versioned keys (name + version): a skill installed as v0.1.0 and then upgraded to
v0.2.0 counts as TWO installs in the per-release stats. Aggregate stats (per-name)
continue to inherit across releases so the marketplace card shows lifetime usage.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


def _version_key(version: str) -> tuple:
    """Semantic-ish sort key: '0.10.0' must sort AFTER '0.2.0' (string order
    would put it before). Non-numeric segments fall back to the raw string."""
    parts = []
    for seg in str(version).split("."):
        try:
            parts.append((0, int(seg)))
        except ValueError:
            parts.append((1, seg))
    return tuple(parts)


class SkillMarketStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        # 按 name + version 区分的 per-release 表 (主键升级: name -> (name, version))
        self._con.execute(
            """CREATE TABLE IF NOT EXISTS skill_meta (
                name TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT '0.1.0',
                install_count INTEGER NOT NULL DEFAULT 0,
                rating_sum REAL NOT NULL DEFAULT 0,
                rating_count INTEGER NOT NULL DEFAULT 0,
                last_installed_at REAL,
                PRIMARY KEY (name, version)
            )"""
        )
        # 迁移: 旧 schema (name 主键, 无 version 列) → (name, version) 复合主键。
        # 缺失迁移会导致 list_skills 等查 version 列报 500 (技能市场「无法连接本地引擎」)。
        cols = [r[1] for r in self._con.execute("PRAGMA table_info(skill_meta)").fetchall()]
        if "version" not in cols:
            self._con.executescript(
                """
                ALTER TABLE skill_meta RENAME TO skill_meta_legacy;
                CREATE TABLE skill_meta (
                    name TEXT NOT NULL,
                    version TEXT NOT NULL DEFAULT '0.1.0',
                    install_count INTEGER NOT NULL DEFAULT 0,
                    rating_sum REAL NOT NULL DEFAULT 0,
                    rating_count INTEGER NOT NULL DEFAULT 0,
                    last_installed_at REAL,
                    PRIMARY KEY (name, version)
                );
                INSERT INTO skill_meta (name, version, install_count, rating_sum, rating_count, last_installed_at)
                    SELECT name, '0.1.0', install_count, rating_sum, rating_count, last_installed_at
                    FROM skill_meta_legacy;
                DROP TABLE skill_meta_legacy;
                """
            )
        self._con.commit()

    # -- installs ----------------------------------------------------------
    def record_install(self, name: str, version: str = "0.1.0") -> None:
        """记录一个具体版本的安装次数。一个 skill 升级版本后安装计数加新的版本。"""
        version = version or "0.1.0"
        with self._lock:
            self._con.execute(
                """INSERT INTO skill_meta (name, version, install_count, last_installed_at)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(name, version) DO UPDATE SET
                     install_count = install_count + 1,
                     last_installed_at = ?""",
                (name, version, time.time(), time.time()),
            )
            self._con.commit()

    # -- ratings -----------------------------------------------------------
    def rate(self, name: str, score: float, version: str = "0.1.0") -> dict:
        """给一个具体版本的 skill 加评分 (1-5); 返回该版本的聚合统计。"""
        score = max(1.0, min(5.0, float(score)))
        version = version or "0.1.0"
        with self._lock:
            self._con.execute(
                """INSERT INTO skill_meta (name, version, rating_sum, rating_count)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(name, version) DO UPDATE SET
                     rating_sum = rating_sum + ?, rating_count = rating_count + 1""",
                (name, version, score, score),
            )
            self._con.commit()
            row = self._con.execute(
                """SELECT install_count, rating_sum, rating_count
                   FROM skill_meta WHERE name=? AND version=?""",
                (name, version),
            ).fetchone()
        return self._aggregate(row)

    # -- per-version stats (精细粒度) -------------------------------------
    def stats(self, name: str, version: str = "0.1.0") -> dict:
        """某 skill 特定版本的统计。"""
        version = version or "0.1.0"
        with self._lock:
            row = self._con.execute(
                """SELECT install_count, rating_sum, rating_count
                   FROM skill_meta WHERE name=? AND version=?""",
                (name, version),
            ).fetchone()
        return self._aggregate(row)

    def versions(self, name: str) -> list[dict]:
        """某 skill 所有版本的统计列表, 按 version (自然语义排序) 返回。"""
        with self._lock:
            rows = self._con.execute(
                """SELECT version, install_count, rating_sum, rating_count, last_installed_at
                   FROM skill_meta WHERE name=? ORDER BY version""",
                (name,),
            ).fetchall()
        # 0.10.0 must sort after 0.2.0 — string order alone is wrong; sort by
        # numeric components (低危: versions() 曾按字典序返回).
        rows = sorted(rows, key=lambda r: _version_key(r[0]))
        out = []
        for version, install_count, rating_sum, rating_count, last_installed_at in rows:
            agg = self._aggregate((install_count, rating_sum, rating_count))
            out.append({
                "version": version,
                **agg,
                "last_installed_at": last_installed_at,
            })
        return out

    # -- per-name aggregate (市场列表页使用) -------------------------------
    def aggregate_stats(self, name: str) -> dict:
        """按 name 聚合: 所有版本安装量相加、评分按 rating_count 加权平均。"""
        with self._lock:
            row = self._con.execute(
                """SELECT SUM(install_count), SUM(rating_sum), SUM(rating_count)
                   FROM skill_meta WHERE name=?""",
                (name,),
            ).fetchone()
        agg = self._aggregate(row)
        # 附加: 版本数 + 最新版本 — MAX(version) is STRING order (0.2.0 > 0.10.0);
        # compute the latest by semantic components instead.
        with self._lock:
            meta = self._con.execute(
                """SELECT COUNT(DISTINCT version), MAX(last_installed_at)
                   FROM skill_meta WHERE name=?""",
                (name,),
            ).fetchone()
            versions = [
                r[0]
                for r in self._con.execute(
                    "SELECT DISTINCT version FROM skill_meta WHERE name=?", (name,)
                )
            ]
        agg["version_count"] = meta[0] or 0
        agg["latest_version"] = (
            max(versions, key=_version_key) if versions else None
        )
        agg["last_installed_at"] = meta[1]
        return agg

    def all_stats(self) -> dict[str, dict]:
        """所有 skill 的 per-name 聚合统计 (SkillsView 列表页使用)。"""
        with self._lock:
            names = self._con.execute(
                "SELECT DISTINCT name FROM skill_meta"
            ).fetchall()
        return {r[0]: self.aggregate_stats(r[0]) for r in names}

    def all_versions_by_name(self) -> dict[str, list[dict]]:
        """所有 skill 的 per-version 条目聚合, 返回 {name: [version_row, ...]}。"""
        with self._lock:
            rows = self._con.execute(
                """SELECT name, version, install_count, rating_sum, rating_count, last_installed_at
                   FROM skill_meta ORDER BY name, version"""
            ).fetchall()
        out: dict[str, list[dict]] = {}
        for name, version, install_count, rating_sum, rating_count, last_installed_at in rows:
            agg = self._aggregate((install_count, rating_sum, rating_count))
            out.setdefault(name, []).append({
                "version": version,
                **agg,
                "last_installed_at": last_installed_at,
            })
        for lst in out.values():
            lst.sort(key=lambda v: _version_key(v["version"]))
        return out

    def all_versioned_stats(self) -> list[dict]:
        """调试用: 返回整张表的 versioned 条目。"""
        with self._lock:
            rows = self._con.execute(
                """SELECT name, version, install_count, rating_sum, rating_count, last_installed_at
                   FROM skill_meta ORDER BY name, version"""
            ).fetchall()
        out = []
        for name, version, install_count, rating_sum, rating_count, last_installed_at in rows:
            agg = self._aggregate((install_count, rating_sum, rating_count))
            out.append({
                "name": name,
                "version": version,
                **agg,
                "last_installed_at": last_installed_at,
            })
        return out

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
            "install_count": int(install_count or 0),
            "rating": round(rating_sum / rating_count, 2) if rating_count else None,
            "rating_count": int(rating_count or 0),
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._con.close()
            except sqlite3.Error:
                pass
