"""Swarm Experience Harness (Refine 机制, 对标 Prime Agent Continual Harness).

Prime Agent 的 Continual Harness 把"补充提示、记忆、技能描述、可复用子代理
规格"存为持久状态, 供 /refine 基于证据小步更新。QunWork 的蜂群 (orchestrator)
在每轮 run 后也会沉淀经验 — 本模块就是那个持久化载体:

  * 每条经验 (SwarmLesson) 有 kind:
      - "lesson"        教训/成功策略 (来自评审结论 + 执行轨迹的蒸馏);
      - "skill_hint"    重复出现的可固化技能提示 (后续可转成真正 skill);
      - "task_template" 可复用任务模板 (意图-任务拆解模式);
  * 每条经验记录来源 run (source_run_id)、意图摘要、内容、命中次数
    (use_count)、时间, 并带版本链 (version) — 经验本身也可被新经验
    修正/回滚 (MemCube 式元数据);
  * 存储: SQLite (workspace/.qunwork/harness.db), 幂等迁移, 线程安全。

下次蜂群规划时, orchestrate 从 harness 检索与当前意图相关的历史经验,
注入 planner 输入 (与 HORNET 共振上下文同机制) — 这就是"自进化"闭环:
跑过的坑下次不再踩, 成功的套路下次直接复用。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_HARNESS_DB_NAME = "harness.db"

LESSON_KINDS = ("lesson", "skill_hint", "task_template")


class SwarmLesson:
    """One distilled piece of swarm experience."""

    __slots__ = (
        "id",
        "kind",
        "title",
        "body",
        "source_run_id",
        "intent",
        "tags",
        "use_count",
        "version",
        "created_at",
        "updated_at",
    )

    def __init__(
        self,
        *,
        id: Optional[int] = None,
        kind: str = "lesson",
        title: str = "",
        body: str = "",
        source_run_id: str = "",
        intent: str = "",
        tags: Optional[list[str]] = None,
        use_count: int = 0,
        version: int = 1,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        self.id = id
        self.kind = kind if kind in LESSON_KINDS else "lesson"
        self.title = title
        self.body = body
        self.source_run_id = source_run_id
        self.intent = intent
        self.tags = tags or []
        self.use_count = use_count
        self.version = version
        self.created_at = created_at
        self.updated_at = updated_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "source_run_id": self.source_run_id,
            "intent": self.intent,
            "tags": self.tags,
            "use_count": self.use_count,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


class HarnessStore:
    """SQLite-backed swarm experience store (workspace/.qunwork/harness.db)."""

    def __init__(self, base_dir: str | Path) -> None:
        base = Path(base_dir)
        base.mkdir(parents=True, exist_ok=True)
        self.path = str(base / _HARNESS_DB_NAME)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS swarm_lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                source_run_id TEXT,
                intent TEXT,
                tags TEXT NOT NULL DEFAULT '[]',
                use_count INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_harness_kind ON swarm_lessons(kind)"
        )
        self._conn.commit()

    def add(
        self,
        *,
        kind: str,
        title: str,
        body: str,
        source_run_id: str = "",
        intent: str = "",
        tags: Optional[list[str]] = None,
    ) -> SwarmLesson:
        """Add a lesson. Same kind+title already exists → bump version & update
        body (new evidence refines old), mirroring /refine's small evidence-backed
        updates (旧版本在 DB 中无独立快照, 但 version 号递增可审计)。"""
        kind = kind if kind in LESSON_KINDS else "lesson"
        title = (title or "").strip()[:200]
        body = (body or "").strip()
        if not title or not body:
            raise ValueError("title and body are required")
        now = _now_iso()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id, version, use_count, body FROM swarm_lessons "
                "WHERE kind = ? AND title = ?",
                (kind, title),
            ).fetchone()
            if existing is not None:
                self._conn.execute(
                    "UPDATE swarm_lessons SET body = ?, intent = ?, version = version + 1, "
                    "updated_at = ? WHERE id = ?",
                    (body, intent, now, existing["id"]),
                )
                self._conn.commit()
                return self.get(existing["id"])  # type: ignore[return-value]
            cur = self._conn.execute(
                "INSERT INTO swarm_lessons "
                "(kind, title, body, source_run_id, intent, tags, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    title,
                    body,
                    source_run_id,
                    intent,
                    json.dumps(tags or [], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return self.get(cur.lastrowid)  # type: ignore[return-value]

    def get(self, lesson_id: int) -> Optional[SwarmLesson]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM swarm_lessons WHERE id = ?", (lesson_id,)
            ).fetchone()
        return _row_to_lesson(row) if row else None

    def list(
        self, *, kind: Optional[str] = None, limit: int = 100
    ) -> list[SwarmLesson]:
        query = "SELECT * FROM swarm_lessons"
        params: list[Any] = []
        if kind is not None:
            query += " WHERE kind = ?"
            params.append(kind)
        query += " ORDER BY use_count DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_lesson(r) for r in rows]

    def search(
        self, query: str, *, kind: Optional[str] = None, k: int = 5
    ) -> list[SwarmLesson]:
        """Keyword search over titles/bodies/tags — cheap, no embedder needed.
        Used by _plan to inject relevant past experience into the planner.

        中文友好: 查询按空白/标点切 token; 对 CJK 连续串额外做 bigram 拆分,
        并保留查询串整体做包含匹配 — "部署测试流程" 能命中含 "部署" 的经验。
        """
        q = (query or "").strip()
        if not q:
            return []
        # 1) 空白/标点 token (英文词 / 中文短词)
        tokens = [
            t
            for t in re.split(r"[\s,，。；;:：/\\|()\[\]{}]+", q.lower())
            if len(t) >= 2
        ]
        # 2) 中文连续串 → 拆 bigram ("部署测试流程" → 部署/署测/测试/试流/流程)
        cjk_run = re.findall(r"[\u4e00-\u9fff]{2,}", q)
        for run in cjk_run:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
        # 3) 去重并保留原始查询整体 (包含匹配)
        tokens = list(dict.fromkeys(tokens))
        lessons = self.list(kind=kind, limit=500)
        if not tokens:
            return []
        scored: list[tuple[float, SwarmLesson]] = []
        for ls in lessons:
            hay = " ".join(
                [ls.title, ls.body, ls.intent, " ".join(ls.tags)]
            ).lower()
            score = sum(hay.count(tok) for tok in tokens)
            if q.lower() in hay:  # 查询整体包含 → 额外加分
                score += 3
            if score:
                scored.append((score, ls))
        scored.sort(key=lambda pair: (-pair[0], pair[1].use_count))
        return [ls for _, ls in scored[:k]]

    def bump_usage(self, lesson_id: int) -> None:
        """Lesson 被注入/命中时刷新 use_count — 高频经验排前 (自进化正反馈)。"""
        with self._lock:
            self._conn.execute(
                "UPDATE swarm_lessons SET use_count = use_count + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (lesson_id,),
            )
            self._conn.commit()

    def delete(self, lesson_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM swarm_lessons WHERE id = ?", (lesson_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # -- S2 记忆系统治理: 经验库生命周期 --------------------------------------
    def maintenance(
        self,
        *,
        dry_run: bool = False,
        min_uses_to_keep: int = 3,
        similar_threshold: float = 0.90,
        max_lessons: int = 500,
    ) -> dict[str, Any]:
        """经验库治理 (S2 记忆系统治理 — 蜂群经验维度的 G1/G2):
          1. 相似去重: 同 kind 且标题/正文高度相似 (difflib >= similar_threshold)
             的重复经验合并 (保留 use_count 高的一条, 其余删除);
          2. 冷经验清理: use_count == 0 且版本 == 1 的"一次性经验"超过
             max_lessons 上限后按最旧优先清理 (经验库不无限膨胀);
          3. 返回 {"removed": [ids], "dry_run": bool}。

        与记忆维护 (coworker/memory/maintenance.py) 对齐的治理周期,
        供 _memory_maintenance_loop 或 manager 手动调用。
        """
        lessons = self.list(limit=max_lessons * 4)
        removed: list[int] = []

        # 1) 相似去重 (同 kind 内)
        from difflib import SequenceMatcher

        by_kind: dict[str, list[SwarmLesson]] = {}
        for ls in lessons:
            by_kind.setdefault(ls.kind, []).append(ls)
        for _kind, members in by_kind.items():
            if len(members) < 2:
                continue
            members.sort(key=lambda ls: (-ls.use_count, ls.id))
            keep: list[SwarmLesson] = [members[0]]
            for dup in members[1:]:
                hay = f"{dup.title} {dup.body}"
                best_score = 0.0
                for k in keep:
                    s = SequenceMatcher(
                        None, f"{k.title} {k.body}", hay
                    ).ratio()
                    if s > best_score:
                        best_score = s
                if best_score >= similar_threshold:
                    removed.append(dup.id)
                    if not dry_run:
                        self.delete(dup.id)
                else:
                    keep.append(dup)

        # 2) 冷经验清理 (上限保护)
        if len(lessons) > max_lessons:
            # 保留活跃经验, 清理零使用的一次性经验
            survivors = sorted(
                lessons,
                key=lambda ls: (
                    ls.use_count > 0,  # 有使用记录优先保留
                    ls.version > 1,  # 更新过的优先保留
                    _parse_iso(ls.created_at) if ls.created_at else 0.0,
                ),
            )
            excess = survivors[: len(survivors) - max_lessons]
            for ls in excess:
                if ls.use_count == 0 and ls.version == 1:
                    removed.append(ls.id)
                    if not dry_run:
                        self.delete(ls.id)

        return {
            "removed": list(dict.fromkeys(removed)),
            "dry_run": dry_run,
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


def _parse_iso(s: str) -> float:
    import time as _t

    try:
        return _t.mktime(_t.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def _row_to_lesson(row: sqlite3.Row) -> SwarmLesson:
    try:
        tags = json.loads(row["tags"] or "[]")
    except (json.JSONDecodeError, TypeError):
        tags = []
    return SwarmLesson(
        id=row["id"],
        kind=row["kind"],
        title=row["title"],
        body=row["body"],
        source_run_id=row["source_run_id"] or "",
        intent=row["intent"] or "",
        tags=tags,
        use_count=int(row["use_count"] or 0),
        version=int(row["version"] or 1),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
