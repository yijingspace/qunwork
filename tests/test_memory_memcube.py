"""P2 MemCube 式元数据 + 睡眠整理 (MemOS MemCube / Dream, 对比文档 #⑤④)。

契约:
  * MemCube 元数据: memories / vector_memories 带 version (版本链) / ttl (过期)
    / origin (来源) / hotness (热度);
  * 版本链: update 快照旧内容, history() 可审计, rollback() 可回滚;
  * TTL: 过期条目从检索/注入隐藏, 睡眠整理统一标记 stale;
  * 睡眠整理 (Dream): TTL 过期清理 + 高频记忆巩固, 合并进 run_maintenance。
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from coworker.memory import Scope, SQLiteMemoryStore
from coworker.memory.maintenance import (
    consolidate_memories,
    consolidate_vector_memories,
    run_maintenance,
)
from coworker.orchestrator import PersistentVectorMemory


def _store(tmp_path):
    return SQLiteMemoryStore(tmp_path / "mem.db")


# -- MemCube 元数据: 默认值 -----------------------------------------------------


def test_new_memory_has_memcube_metadata(tmp_path):
    store = _store(tmp_path)
    item = store.add("fact", scope=Scope.WORKSPACE, workspace="/p", origin="remember")
    assert item.version == 1
    assert item.origin == "remember"
    assert item.ttl is None
    assert item.hotness == 0.0
    assert item.use_count == 0


def test_origin_and_ttl_stored(tmp_path):
    store = _store(tmp_path)
    item = store.add(
        "temporary note",
        workspace="/p",
        origin="import",
        ttl="2099-01-01 00:00:00",
    )
    loaded = store.get(item.id)
    assert loaded.origin == "import"
    assert loaded.ttl == "2099-01-01 00:00:00"


# -- 版本链 --------------------------------------------------------------------


def test_update_creates_version_chain(tmp_path):
    store = _store(tmp_path)
    item = store.add("v1 content", workspace="/p")
    store.update(item.id, "v2 content")
    store.update(item.id, "v3 content")
    hist = store.history(item.id)
    assert len(hist) == 3  # v1, v2, v3
    assert [h["version"] for h in hist] == [1, 2, 3]
    assert hist[-1]["current"] is True
    assert store.get(item.id).version == 3


def test_update_preserves_history_content(tmp_path):
    store = _store(tmp_path)
    item = store.add("original", workspace="/p")
    store.update(item.id, "revised")
    hist = store.history(item.id)
    assert hist[0]["content"] == "original"
    assert hist[1]["content"] == "revised"


def test_rollback_restores_version(tmp_path):
    store = _store(tmp_path)
    item = store.add("v1", workspace="/p")
    store.update(item.id, "v2")
    store.update(item.id, "v3")
    rolled = store.rollback(item.id, 1)
    assert rolled is not None
    assert rolled.content == "v1"
    assert rolled.version == 4  # 回滚本身也是一次新版本
    # 版本链保留了完整历史
    assert len(store.history(item.id)) == 4


def test_rollback_unknown_version_noop(tmp_path):
    store = _store(tmp_path)
    item = store.add("v1", workspace="/p")
    store.update(item.id, "v2")
    assert store.rollback(item.id, 99) is None
    assert store.get(item.id).content == "v2"  # 未改动


def test_rollback_missing_memory(tmp_path):
    store = _store(tmp_path)
    assert store.rollback(12345, 1) is None


# -- hotness -------------------------------------------------------------------


def test_bump_usage_raises_hotness(tmp_path):
    store = _store(tmp_path)
    item = store.add("fact", workspace="/p")
    store.bump_usage(item.id)
    assert store.get(item.id).use_count == 1
    assert store.get(item.id).hotness == pytest.approx(0.5, abs=0.01)
    for _ in range(4):
        store.bump_usage(item.id)
    assert store.get(item.id).hotness >= 0.8  # 频繁命中趋近常青


# -- TTL -----------------------------------------------------------------------


def test_ttl_expired_hidden_from_list(tmp_path):
    store = _store(tmp_path)
    item = store.add("old note", workspace="/p", ttl="2020-01-01 00:00:00")
    # list 不主动过滤 ttl (由睡眠整理标记 stale), 但 get 可见
    assert store.get(item.id).content == "old note"


def test_consolidate_marks_ttl_expired_stale(tmp_path):
    store = _store(tmp_path)
    item = store.add("expired note", workspace="/p", ttl="2020-01-01 00:00:00")
    result = consolidate_memories(store)
    assert result["expired"] == [item.id]
    assert store.list() == []  # stale 隐藏
    assert store.list(include_stale=True)[0].stale is True


def test_consolidate_keeps_live_ttl(tmp_path):
    store = _store(tmp_path)
    item = store.add("live note", workspace="/p", ttl="2099-01-01 00:00:00")
    result = consolidate_memories(store)
    assert result["expired"] == []
    assert store.list()[0].id == item.id


def test_consolidate_dry_run_does_not_mark(tmp_path):
    store = _store(tmp_path)
    item = store.add("expired note", workspace="/p", ttl="2020-01-01 00:00:00")
    result = consolidate_memories(store, dry_run=True)
    assert result["expired"] == [item.id]
    assert store.list()[0].id == item.id  # 未标记


# -- 睡眠整理: 高频记忆巩固 ----------------------------------------------------


def test_consolidate_revives_hot_stale_memory(tmp_path):
    store = _store(tmp_path)
    item = store.add("important old fact", workspace="/p")
    store._conn.execute(
        "UPDATE memories SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
        (item.id,),
    )
    # 高频使用 → hotness 高 → stale 后应被巩固 (复活)
    for _ in range(6):
        store.bump_usage(item.id)
    store.mark_stale(item.id)  # 模拟之前被标记 stale
    result = consolidate_memories(store)
    assert result["consolidated"] == [item.id]
    assert store.list()[0].id == item.id  # 复活可见


def test_consolidate_keeps_cold_stale_hidden(tmp_path):
    store = _store(tmp_path)
    item = store.add("cold fact", workspace="/p")
    store._conn.execute(
        "UPDATE memories SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
        (item.id,),
    )
    store.mark_stale(item.id)  # 冷且未使用 → 保持隐藏
    result = consolidate_memories(store)
    assert result["consolidated"] == []
    assert store.list() == []


# -- 向量记忆 MemCube 元数据 ---------------------------------------------------


def _vstore(tmp_path, scope="ws"):
    db = tmp_path / "vmem.db"
    m = PersistentVectorMemory(db, scope=scope)
    m.close()
    return db


def test_vector_add_origin_ttl_persisted(tmp_path):
    db = _vstore(tmp_path)
    m = PersistentVectorMemory(db, scope="ws")
    m.add("lesson", origin="swarm", ttl=time.time() + 86400)
    m.close()
    con = sqlite3.connect(str(db))
    row = con.execute(
        "SELECT meta, ttl FROM vector_memories WHERE id=1"
    ).fetchone()
    con.close()
    assert json.loads(row[0])["origin"] == "swarm"
    assert row[1] is not None


def test_vector_ttl_expired_not_loaded(tmp_path):
    db = _vstore(tmp_path)
    m = PersistentVectorMemory(db, scope="ws")
    m.add("expired lesson", ttl=time.time() - 100)  # 已过期
    m.close()
    m2 = PersistentVectorMemory(db, scope="ws")
    assert len(m2) == 0  # 过期条目不载入 → 检索不可见
    m2.close()
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT COUNT(*) FROM vector_memories").fetchone()[0] == 1  # 保留
    con.close()


def test_vector_consolidate_marks_expired_stale(tmp_path):
    db = _vstore(tmp_path)
    m = PersistentVectorMemory(db, scope="ws")
    m.add("expired lesson", ttl=time.time() - 100)
    m.close()
    result = consolidate_vector_memories(db)
    assert result["expired"] == [1]
    con = sqlite3.connect(str(db))
    meta = json.loads(
        con.execute("SELECT meta FROM vector_memories WHERE id=1").fetchone()[0]
    )
    con.close()
    assert meta.get("stale") is True


# -- run_maintenance 汇总 ------------------------------------------------------


def test_run_maintenance_includes_consolidation(tmp_path):
    store = _store(tmp_path)
    store.add("expired note", workspace="/p", ttl="2020-01-01 00:00:00")
    result = run_maintenance(store)
    assert result["memories_consolidate"]["expired"] == [1]
    assert "memories_dedupe" in result and "memories_decay" in result
