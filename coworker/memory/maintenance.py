"""P1/P2 记忆维护 — 去重合并 + 衰减遗忘 + MemCube 元数据 + 睡眠整理.

依据 QunWork 知识库研究文档 与 MemOS 对比研究的 MemCube 方案:
  * GuaAgent 框架研究 (#112): 记忆全量存储 + 智能整理, "存得全, 用得精";
  * OpenClaw 蜂群架构重构构想 (#141): FADEMEM 差分衰减遗忘 — 记忆重要性得分
    I = α·rel(c,Q) + β·f/(1+f) + γ·recency(t), 记忆按重要性动态分配
    长期/短期层级; Manus "遗忘内容保留路径" — 从 L1(上下文) 降级到
    L2(文件系统)/L3(知识库), 而不是抹掉;
  * MemOS MemCube: 每个记忆单元带 version (版本链) / ttl / origin / hotness,
    为记忆可审计、可回滚打底; Dream 睡眠巩固 (离线整理).

本模块提供维护原语, 都可在后台低频任务里跑:

  * 去重合并 (dedupe): 同 key / 内容高度相似的记忆合并为一条, 保留较新或
    较完整的, 并把被合并条目的 id 记入映射 (旧 id -> 新 id), 模型引用旧 id
    时仍能定位到合并后的内容.
  * 衰减遗忘 (decay): 基于最后使用时间与访问频率衰减记忆的新鲜度, 长期
    未用 / 低价值的记忆自动降级 (标记 stale, 从注入与检索中隐藏但保留在库
    中, 可恢复 — 对应 Manus 的"降级而非抹除").
  * 睡眠整理 (consolidate, 对应 MemOS Dream): TTL 过期条目标记 stale +
    高频记忆 (hotness 高) 巩固复活, 与衰减同周期运行.

结构化记忆 (memories 表) 与向量记忆 (vector_memories 表) 都覆盖.
所有函数都是幂等的、可安全重复运行的; 默认保守 (低相似度阈值、长半衰期).
"""

from __future__ import annotations

import datetime as _dt
import difflib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# -- 去重阈值 ---------------------------------------------------------------
# 内容归一化后完全相同的记忆必定合并 (保守下限)。
TEXT_IDENTICAL_NORM = True
# 无 key 时, 内容相似度达到该阈值即合并 (difflib ratio)。
MEMORY_SIM_THRESHOLD = 0.92
# 向量记忆文本相似度阈值 (difflib ratio, 无 embedder 时用)。
VECTOR_SIM_THRESHOLD = 0.90
# 带 key 的记忆, 内容相似度达到该阈值才合并 (同 key 但内容差异大 → 保留两条)。
KEYED_SIM_THRESHOLD = 0.70

# -- 衰减参数 (半衰期模型) ---------------------------------------------------
# 记忆最后一次使用后, 新鲜度按指数衰减: freshness = exp(-ln2 * age / HALF_LIFE_DAYS)
HALF_LIFE_DAYS = 90.0
# 低于该新鲜度视为 stale (从注入/检索隐藏, 但保留在库中可恢复)。
STALE_THRESHOLD = 0.20
# 衰减 pass 一次最多处理/标记的条数上限 (保护大库)。
DECAY_BATCH_LIMIT = 2000


def _norm(text: str) -> str:
    """归一化: 去首尾空白、折叠空白、转小写 — 用于"内容完全相同"判定。"""
    return " ".join((text or "").split()).lower()


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    na, nb = _norm(a), _norm(b)
    # 包含关系 (一方是另一方的子串/子集) → 视为高度相似: "prefers pnpm"
    # 就是 "user prefers pnpm over npm" 的语义子集。
    if na in nb or nb in na:
        return 0.95
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _age_days(created_at: Any, now: Optional[float] = None) -> float:
    """memories.created_at 是 SQLite CURRENT_TIMESTAMP 文本 (UTC);
    vector_memories.created_at 是 epoch 秒。统一返回天数。"""
    now = time.time() if now is None else now
    if isinstance(created_at, (int, float)):
        return max(0.0, (now - float(created_at)) / 86400.0)
    if isinstance(created_at, str) and created_at:
        try:
            ts = _dt.datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=_dt.timezone.utc
            )
            return max(0.0, (now - ts.timestamp()) / 86400.0)
        except ValueError:
            return 0.0
    return 0.0


# -- 结构化记忆 (memories 表) -------------------------------------------------

def dedupe_memories(store: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """合并 memories 表中的重复记忆。

    规则 (按优先级):
      1. 同 (scope, workspace, key) 且 key 非空, 且内容归一化相同 → 保留最小 id;
      2. 同 (scope, workspace, key) 且 key 非空, 内容相似度 >= KEYED_SIM_THRESHOLD
         → 保留内容较长的一条 (更完整), 其余合并;
      3. 无 key 的记忆, 内容归一化相同 → 保留最小 id;
      4. 无 key 的记忆, 内容相似度 >= MEMORY_SIM_THRESHOLD → 保留较长一条。

    返回 {"merged": {old_id: new_id}, "removed": [ids], "dry_run": bool}。
    """
    # include_stale=True: 衰减隐藏的冷记忆也要参与去重 (避免重复堆积后无人清理)。
    try:
        items = store.list(include_stale=True)
    except TypeError:
        items = store.list()  # 旧适配器无 include_stale 参数
    groups: dict[tuple[str, str, str], list[Any]] = {}
    for it in items:
        g = (
            it.scope.value if hasattr(it.scope, "value") else str(it.scope),
            it.workspace or "",
            (it.key or "").strip().lower(),
        )
        groups.setdefault(g, []).append(it)

    merged: dict[int, int] = {}
    removed: list[int] = []
    for g, members in groups.items():
        if len(members) < 2:
            continue
        keyed = bool(g[2])
        # sort by id for deterministic keep-min behavior
        members.sort(key=lambda it: it.id)
        kept = members[0]
        for dup in members[1:]:
            keep_this = False
            if _norm(kept.content) == _norm(dup.content):
                keep_this = True  # identical → merge into kept
            elif keyed:
                if _similar(kept.content, dup.content) >= KEYED_SIM_THRESHOLD:
                    keep_this = True
            else:
                if _similar(kept.content, dup.content) >= MEMORY_SIM_THRESHOLD:
                    keep_this = True
            if keep_this:
                # keep the longer (more complete) content
                if len(dup.content) > len(kept.content):
                    if not dry_run:
                        store.update(kept.id, dup.content)
                merged[dup.id] = kept.id
                removed.append(dup.id)
                if not dry_run:
                    store.delete(dup.id)
            else:
                # content differs enough → this member becomes its own keeper
                kept = dup
    return {"merged": merged, "removed": removed, "dry_run": dry_run}


def decay_memories(
    store: Any,
    *,
    half_life_days: float = HALF_LIFE_DAYS,
    stale_threshold: float = STALE_THRESHOLD,
    dry_run: bool = False,
) -> dict[str, Any]:
    """衰减 memories 表中长期未使用的记忆, 标记 stale (从注入隐藏)。

    新鲜度 = max(年龄衰减, 使用频率地板), 对应 FADEMEM:
      * 年龄衰减: freshness = exp(-ln2 * age / half_life);
      * 频率地板: f 次使用 → 地板 0.8*(1 - 1/(1+f)) — 高频使用的记忆
        即使很老也保持可检索 (常青), 冷记忆按年龄继续衰减。
    低于 stale_threshold 的记忆标记 stale (从注入/检索隐藏, 保留在库中
    可恢复 — Manus"降级而非抹除")。

    返回 {"stale": [ids], "freshness": {id: value}, "dry_run": bool}。
    """
    items = store.list()
    if len(items) > DECAY_BATCH_LIMIT:
        items = items[:DECAY_BATCH_LIMIT]
    now = time.time()
    stale: list[int] = []
    fresh_map: dict[int, float] = {}
    has_use_count = hasattr(store, "bump_usage")

    for it in items:
        age = _age_days(it.created_at, now)
        freshness = 2.0 ** (-age / half_life_days)
        if has_use_count:
            f = float(getattr(it, "use_count", 0) or 0)
            # FADEMEM: 访问频率 f 提供新鲜度地板 — 高频使用的记忆不该被遗忘。
            # f=1 → 地板 0.35; f>=4 → 地板 0.8 (基本常青)。年龄衰减与频率
            # 地板取 max: 一个被频繁命中的老记忆保持可检索, 冷记忆继续衰减。
            usage_floor = 0.8 * (1.0 - 1.0 / (1.0 + f))
            freshness = max(freshness, usage_floor)
        fresh_map[it.id] = round(freshness, 4)
        if freshness < stale_threshold:
            stale.append(it.id)
            if not dry_run:
                store.mark_stale(it.id)
    return {"stale": stale, "freshness": fresh_map, "dry_run": dry_run}


# -- 向量记忆 (vector_memories 表) ---------------------------------------------

def _vector_db_path(db_path: str | Path) -> str:
    return str(db_path)


def _vector_rows(db_path: str | Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(_vector_db_path(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, scope, text, meta, vector, created_at, "
            "COALESCE(use_count,0) AS use_count, "
            "COALESCE(last_used_at, created_at) AS last_used_at, "
            "ttl, COALESCE(hotness, 0.0) AS hotness "
            "FROM vector_memories ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def dedupe_vector_memories(
    db_path: str | Path, *, dry_run: bool = False
) -> dict[str, Any]:
    """合并 vector_memories 表中同 scope 内文本高度相似的记忆。

    用 difflib 文本相似度 (无需 embedder 也能跑; 低频任务, O(n²) 可接受)。
    保留 id 较小 (更早) 的一条; 较晚的重复条目删除。
    """
    rows = _vector_rows(db_path)
    if not rows:
        return {"merged": {}, "removed": [], "dry_run": dry_run}

    by_scope: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_scope.setdefault(r["scope"], []).append(r)

    merged: dict[int, int] = {}
    removed: list[int] = []
    con = sqlite3.connect(_vector_db_path(db_path))
    try:
        for _scope, members in by_scope.items():
            if len(members) < 2:
                continue
            members.sort(key=lambda r: r["id"])
            keep: list[dict[str, Any]] = [members[0]]
            for dup in members[1:]:
                dup_text = dup["text"]
                best = max(
                    (_similar(k["text"], dup_text), k) for k in keep
                )
                score, keeper = best
                if score >= VECTOR_SIM_THRESHOLD:
                    merged[dup["id"]] = keeper["id"]
                    removed.append(dup["id"])
                    if not dry_run:
                        con.execute("DELETE FROM vector_memories WHERE id = ?", (dup["id"],))
                else:
                    keep.append(dup)
            if not dry_run:
                con.commit()
    finally:
        con.close()
    return {"merged": merged, "removed": removed, "dry_run": dry_run}


def decay_vector_memories(
    db_path: str | Path,
    *,
    half_life_days: float = HALF_LIFE_DAYS,
    stale_threshold: float = STALE_THRESHOLD,
    dry_run: bool = False,
) -> dict[str, Any]:
    """衰减 vector_memories 中长期未使用的条目, 标记 stale (检索时隐藏)。

    新鲜度 = max(年龄衰减, 使用频率地板), 同 decay_memories (FADEMEM):
    age 用 last_used_at (无则 created_at), f 为 use_count。stale 条目以
    meta 标记 ({"stale": true}) — 不删除, 检索层 (PersistentVectorMemory
    加载时) 过滤, 数据保留可恢复。
    """
    rows = _vector_rows(db_path)
    if not rows:
        return {"stale": [], "freshness": {}, "dry_run": dry_run}
    now = time.time()
    stale: list[int] = []
    fresh_map: dict[int, float] = {}
    con = sqlite3.connect(_vector_db_path(db_path))
    try:
        for r in rows:
            age = _age_days(r["last_used_at"], now)
            freshness = 2.0 ** (-age / half_life_days)
            f = float(r["use_count"] or 0)
            usage_floor = 0.8 * (1.0 - 1.0 / (1.0 + f))
            freshness = max(freshness, usage_floor)
            fresh_map[r["id"]] = round(freshness, 4)
            if freshness < stale_threshold:
                stale.append(r["id"])
                if not dry_run:
                    meta = json.loads(r["meta"] or "{}")
                    if not meta.get("stale"):
                        meta["stale"] = True
                        con.execute(
                            "UPDATE vector_memories SET meta = ? WHERE id = ?",
                            (json.dumps(meta, ensure_ascii=False), r["id"]),
                        )
        if not dry_run:
            con.commit()
    finally:
        con.close()
    return {"stale": stale, "freshness": fresh_map, "dry_run": dry_run}


# -- P2 睡眠整理 (Dream consolidation, 对应 MemOS Dream) -----------------------

def consolidate_memories(
    store: Any,
    *,
    dry_run: bool = False,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """睡眠整理 (Dream): 记忆 consolidation pass。

    对应 MemOS Dream 睡眠巩固 + QunWork 知识库文档的"智能整理"理念:
      1. TTL 过期检查: 设置了 ttl 且已过期的记忆 → 标记 stale (隐藏),
         由后续衰减 pass 保持; 数据保留可恢复;
      2. 高频记忆巩固: hotness >= 0.75 (约 >=3 次命中) 的记忆解除 stale
         (巩固为常青记忆), 与衰减的频率地板配合;
      3. 返回 consolidation 统计 (auditable summary)。

    这是纯整理操作 — 不做破坏性删除, 全部可逆 (Manus 降级而非抹除)。
    """
    now = time.time() if now is None else now
    try:
        items = store.list(include_stale=True)
    except TypeError:
        items = store.list()
    expired: list[int] = []
    consolidated: list[int] = []
    for it in items:
        # 1) TTL 过期 → stale
        if it.ttl:
            try:
                ts = _dt.datetime.strptime(it.ttl, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=_dt.timezone.utc
                )
                if now >= ts.timestamp():
                    expired.append(it.id)
                    if not dry_run:
                        store.mark_stale(it.id)
                    continue
            except (ValueError, TypeError):
                pass  # 无法解析的 ttl 视为未设置
        # 2) 高频记忆巩固 (hotness 来自 bump_usage 的 1 - 1/(1+use_count))
        hotness = float(getattr(it, "hotness", 0.0) or 0.0)
        if it.stale and hotness >= 0.75:
            consolidated.append(it.id)
            if not dry_run:
                store.mark_stale(it.id, False)
    return {
        "expired": expired,
        "consolidated": consolidated,
        "dry_run": dry_run,
    }


def consolidate_vector_memories(
    db_path: str | Path,
    *,
    dry_run: bool = False,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """睡眠整理 (Dream) — vector_memories 版: TTL 过期标记 stale + 高频巩固。

    vector 记忆的 stale 用 meta.stale 标记 (检索层过滤); TTL 用 ttl 列
    (epoch 秒), 由 PersistentVectorMemory._load 在加载时跳过。
    """
    rows = _vector_rows(db_path)
    if not rows:
        return {"expired": [], "consolidated": [], "dry_run": dry_run}
    now = time.time() if now is None else now
    expired: list[int] = []
    consolidated: list[int] = []
    con = sqlite3.connect(_vector_db_path(db_path))
    try:
        for r in rows:
            ttl = r.get("ttl")
            if ttl is not None:
                try:
                    if now >= float(ttl):
                        expired.append(r["id"])
                        if not dry_run:
                            meta = json.loads(r["meta"] or "{}")
                            if not meta.get("stale"):
                                meta["stale"] = True
                                con.execute(
                                    "UPDATE vector_memories SET meta = ? WHERE id = ?",
                                    (json.dumps(meta, ensure_ascii=False), r["id"]),
                                )
                        continue
                except (TypeError, ValueError):
                    pass
            meta = json.loads(r["meta"] or "{}")
            hotness = float(r.get("hotness") or 0.0)
            if meta.get("stale") and hotness >= 0.75:
                consolidated.append(r["id"])
                if not dry_run:
                    meta.pop("stale", None)
                    con.execute(
                        "UPDATE vector_memories SET meta = ? WHERE id = ?",
                        (json.dumps(meta, ensure_ascii=False), r["id"]),
                    )
        if not dry_run:
            con.commit()
    finally:
        con.close()
    return {"expired": expired, "consolidated": consolidated, "dry_run": dry_run}


def run_maintenance(
    memory_store: Any,
    vector_db_path: Optional[str | Path] = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """统一入口: 一次跑完全部维护 (去重 + 衰减 + 睡眠整理)。返回汇总。

    供后台任务 (_memory_maintenance_loop) 与 manager.memory_maintenance 调用。
    """
    result: dict[str, Any] = {
        "memories_dedupe": dedupe_memories(memory_store, dry_run=dry_run),
        "memories_decay": decay_memories(memory_store, dry_run=dry_run),
        # P2 睡眠整理 (Dream): TTL 过期清理 + 高频记忆巩固, 与衰减同周期。
        "memories_consolidate": consolidate_memories(memory_store, dry_run=dry_run),
    }
    if vector_db_path is not None:
        result["vector_dedupe"] = dedupe_vector_memories(vector_db_path, dry_run=dry_run)
        result["vector_decay"] = decay_vector_memories(vector_db_path, dry_run=dry_run)
        result["vector_consolidate"] = consolidate_vector_memories(
            vector_db_path, dry_run=dry_run
        )
    return result
