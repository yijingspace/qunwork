"""P1 记忆维护 — 自动去重合并 + 衰减/遗忘管理 (GuaAgent/OpenClaw 文档)。

契约:
  * 去重: 同 key / 内容高度相似的记忆合并为一条, 返回 old_id -> new_id 映射;
  * 衰减: 长期未用 / 低新鲜度的记忆标记 stale (从注入隐藏, 不删除, 可恢复);
  * 向量记忆: 同 scope 高相似度文本去重; 低新鲜度条目 meta 标记 stale;
  * 全部函数幂等, dry_run 不写库。
"""

from __future__ import annotations

import json
import sqlite3

from coworker.memory import Scope, SQLiteMemoryStore
from coworker.memory.maintenance import (
    MEMORY_SIM_THRESHOLD,
    VECTOR_SIM_THRESHOLD,
    decay_memories,
    decay_vector_memories,
    dedupe_memories,
    dedupe_vector_memories,
    run_maintenance,
)
from coworker.orchestrator import PersistentVectorMemory


def _store(tmp_path):
    return SQLiteMemoryStore(tmp_path / "mem.db")


def _vstore(tmp_path, scope="ws"):
    db = tmp_path / "vmem.db"
    m = PersistentVectorMemory(db, scope=scope)
    m.close()
    return db


# -- 结构化记忆去重 ------------------------------------------------------------


def test_dedupe_identical_keyed_memories(tmp_path):
    store = _store(tmp_path)
    a = store.add("user prefers pnpm", scope=Scope.WORKSPACE, key="pkg_manager", workspace="/p")
    b = store.add("user prefers pnpm", scope=Scope.WORKSPACE, key="pkg_manager", workspace="/p")
    result = dedupe_memories(store)
    assert result["removed"] == [b.id]
    assert result["merged"] == {b.id: a.id}
    remaining = store.list(include_stale=True)
    assert [m.id for m in remaining] == [a.id]


def test_dedupe_keeps_longer_keyed_memory(tmp_path):
    store = _store(tmp_path)
    short = store.add("prefers pnpm", scope=Scope.WORKSPACE, key="pkg", workspace="/p")
    long = store.add("user prefers pnpm over npm for monorepos", scope=Scope.WORKSPACE, key="pkg", workspace="/p")
    result = dedupe_memories(store)
    assert result["removed"] == [long.id]  # 较晚的重复被合并
    assert result["merged"] == {long.id: short.id}
    kept = store.get(short.id)
    assert kept.content == "user prefers pnpm over npm for monorepos"  # 内容取更完整的一条
    assert store.get(long.id) is None


def test_dedupe_identical_unkeyed_memories(tmp_path):
    store = _store(tmp_path)
    a = store.add("deploys on Fridays are banned", workspace="/p")
    b = store.add("Deploys  on  Fridays   are banned", workspace="/p")  # whitespace norm
    result = dedupe_memories(store)
    assert result["removed"] == [b.id]
    assert result["merged"] == {b.id: a.id}


def test_dedupe_similar_unkeyed_memories(tmp_path):
    store = _store(tmp_path)
    a = store.add("the deploy freeze happens every Friday on production systems", workspace="/p")
    b = store.add("the deploy freeze happens every Friday on production systems worldwide", workspace="/p")
    result = dedupe_memories(store)
    assert result["removed"] == [b.id]  # 包含关系 → 高度相似


def test_dedupe_keeps_distinct_memories(tmp_path):
    store = _store(tmp_path)
    a = store.add("uses pnpm", workspace="/p")
    b = store.add("prefers dark mode", workspace="/p")
    result = dedupe_memories(store)
    assert result["removed"] == []
    assert len(store.list(include_stale=True)) == 2


def test_dedupe_dry_run_does_not_write(tmp_path):
    store = _store(tmp_path)
    a = store.add("same fact", workspace="/p")
    b = store.add("same fact", workspace="/p")
    result = dedupe_memories(store, dry_run=True)
    assert result["dry_run"] is True
    assert result["removed"] == [b.id]
    assert len(store.list(include_stale=True)) == 2  # 未删除


def test_dedupe_scope_isolation(tmp_path):
    store = _store(tmp_path)
    a = store.add("same fact", workspace="/p1")
    b = store.add("same fact", workspace="/p2")
    result = dedupe_memories(store)
    assert result["removed"] == []  # 不同 workspace 不合并


# -- 结构化记忆衰减 ------------------------------------------------------------


def test_decay_marks_old_unused_memory_stale(tmp_path):
    store = _store(tmp_path)
    item = store.add("ancient fact", workspace="/p")
    # 伪造一个很老的 created_at
    store._conn.execute(
        "UPDATE memories SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
        (item.id,),
    )
    store._conn.commit()
    result = decay_memories(store)
    assert result["stale"] == [item.id]
    # 默认 list 隐藏 stale
    assert store.list() == []
    assert store.list(include_stale=True)[0].stale is True


def test_decay_keeps_recent_memory(tmp_path):
    store = _store(tmp_path)
    item = store.add("fresh fact", workspace="/p")
    result = decay_memories(store)
    assert result["stale"] == []
    assert store.list()[0].id == item.id


def test_decay_frequent_usage_resists_stale(tmp_path):
    store = _store(tmp_path)
    item = store.add("old but used fact", workspace="/p")
    store._conn.execute(
        "UPDATE memories SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
        (item.id,),
    )
    store.bump_usage(item.id)  # 被命中过
    for _ in range(10):
        store.bump_usage(item.id)
    result = decay_memories(store)
    assert result["stale"] == []  # 高频访问对抗年龄衰减


def test_decay_dry_run_does_not_mark(tmp_path):
    store = _store(tmp_path)
    item = store.add("ancient fact", workspace="/p")
    store._conn.execute(
        "UPDATE memories SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
        (item.id,),
    )
    store._conn.commit()
    result = decay_memories(store, dry_run=True)
    assert result["dry_run"] is True
    assert store.list()[0].id == item.id  # 未被标记


def test_stale_recoverable_via_mark_stale_false(tmp_path):
    """Manus '降级而非抹除': stale 记忆可恢复。"""
    store = _store(tmp_path)
    item = store.add("old fact", workspace="/p")
    store._conn.execute(
        "UPDATE memories SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
        (item.id,),
    )
    store._conn.commit()
    decay_memories(store)
    assert store.list() == []
    store.mark_stale(item.id, False)  # 恢复
    assert store.list()[0].id == item.id


# -- 向量记忆去重 --------------------------------------------------------------


def test_vector_dedupe_similar_text(tmp_path):
    db = _vstore(tmp_path)
    m = PersistentVectorMemory(db, scope="ws")
    m.add("Write a quarterly report for the board")
    m.add("Write a quarterly report for the board")
    m.close()
    result = dedupe_vector_memories(db)
    assert len(result["removed"]) == 1
    con = sqlite3.connect(str(db))
    count = con.execute("SELECT COUNT(*) FROM vector_memories").fetchone()[0]
    con.close()
    assert count == 1


def test_vector_dedupe_keeps_distinct(tmp_path):
    db = _vstore(tmp_path)
    m = PersistentVectorMemory(db, scope="ws")
    m.add("Write a quarterly report")
    m.add("Refactor the payment module")
    m.close()
    result = dedupe_vector_memories(db)
    assert result["removed"] == []


def test_vector_dedupe_scope_isolation(tmp_path):
    db = tmp_path / "vmem.db"
    m1 = PersistentVectorMemory(db, scope="ws1")
    m1.add("shared text content here")
    m2 = PersistentVectorMemory(db, scope="ws2")
    m2.add("shared text content here")
    m1.close(); m2.close()
    result = dedupe_vector_memories(db)
    assert result["removed"] == []  # 不同 scope 不合并


def test_vector_dedupe_dry_run(tmp_path):
    db = _vstore(tmp_path)
    m = PersistentVectorMemory(db, scope="ws")
    m.add("duplicate text line")
    m.add("duplicate text line")
    m.close()
    result = dedupe_vector_memories(db, dry_run=True)
    assert result["dry_run"] is True
    con = sqlite3.connect(str(db))
    count = con.execute("SELECT COUNT(*) FROM vector_memories").fetchone()[0]
    con.close()
    assert count == 2


# -- 向量记忆衰减 --------------------------------------------------------------


def _set_vm_old(db, item_id, created_at_ts=1577836800.0):  # 2020-01-01
    con = sqlite3.connect(str(db))
    con.execute("UPDATE vector_memories SET created_at = ? WHERE id = ?", (created_at_ts, item_id))
    con.commit()
    con.close()


def test_vector_decay_marks_old_stale(tmp_path):
    db = _vstore(tmp_path)
    m = PersistentVectorMemory(db, scope="ws")
    m.add("ancient lesson about deployments")
    old_id = 1
    m.close()
    _set_vm_old(db, old_id)
    result = decay_vector_memories(db)
    assert result["stale"] == [old_id]
    con = sqlite3.connect(str(db))
    meta = json.loads(con.execute("SELECT meta FROM vector_memories WHERE id=?", (old_id,)).fetchone()[0])
    con.close()
    assert meta.get("stale") is True


def test_vector_decay_keeps_recent(tmp_path):
    db = _vstore(tmp_path)
    m = PersistentVectorMemory(db, scope="ws")
    m.add("recent lesson")
    m.close()
    result = decay_vector_memories(db)
    assert result["stale"] == []


# -- 统一入口 ------------------------------------------------------------------


def test_run_maintenance_covers_both_stores(tmp_path):
    store = _store(tmp_path)
    store.add("dup fact", workspace="/p")
    store.add("dup fact", workspace="/p")
    db = _vstore(tmp_path)
    m = PersistentVectorMemory(db, scope="ws")
    m.add("duplicate vector text")
    m.add("duplicate vector text")
    m.close()

    result = run_maintenance(store, db)
    assert result["memories_dedupe"]["removed"]
    assert result["vector_dedupe"]["removed"]
    assert "memories_decay" in result and "vector_decay" in result


# -- manager 集成 --------------------------------------------------------------


def test_manager_memory_maintenance_wires_both_stores(tmp_path):
    """SessionManager.memory_maintenance 覆盖结构化记忆 + 默认工作区向量记忆,
    并返回可审计摘要。"""
    from coworker.conversations import ConversationStore
    from coworker.server.manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr.session_store = ConversationStore(tmp_path / "conv.db")
    mgr.memory_store = _store(tmp_path)
    mgr.default_workspace = str(tmp_path)
    mgr.memory_store.add("dup fact", workspace=str(tmp_path))
    mgr.memory_store.add("dup fact", workspace=str(tmp_path))

    # 向量记忆在默认工作区 .qunwork/memory.db
    vdb = tmp_path / ".qunwork" / "memory.db"
    vm = PersistentVectorMemory(vdb, scope=str(tmp_path))
    vm.add("duplicate vector text")
    vm.add("duplicate vector text")
    vm.close()

    result = mgr.memory_maintenance()
    assert result["memories_dedupe"]["removed"]
    assert result["vector_dedupe"]["removed"]
    # dry_run 不写库
    result2 = mgr.memory_maintenance(dry_run=True)
    assert result2["memories_dedupe"]["dry_run"] is True
    assert len(mgr.memory_store.list(include_stale=True)) == 1


def test_stale_memory_hidden_from_engine_injection(tmp_path):
    """衰减为 stale 的记忆不再注入引擎系统提示 (Manus 降级); 恢复后重新可见。"""
    from coworker.agent import build_code_engine
    from coworker.providers import ModelCapabilities, ProviderClient

    class Stub(ProviderClient):
        def complete(self, **kwargs):  # pragma: no cover - not invoked
            raise NotImplementedError

        def capabilities(self, model):
            return ModelCapabilities()

    store = _store(tmp_path)
    item = store.add("ancient preference", workspace=str(tmp_path.resolve()))
    store._conn.execute(
        "UPDATE memories SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
        (item.id,),
    )
    store._conn.commit()
    decay_memories(store)
    assert store.list() == []  # stale 隐藏

    engine = build_code_engine(
        workspace=tmp_path, provider=Stub(), memory_store=store
    )
    try:
        assert "ancient preference" not in engine.messages[0]["content"]
    finally:
        engine.executor.close()

    # 恢复后重新注入
    store.mark_stale(item.id, False)
    engine2 = build_code_engine(
        workspace=tmp_path, provider=Stub(), memory_store=store
    )
    try:
        assert "ancient preference" in engine2.messages[0]["content"]
    finally:
        engine2.executor.close()
