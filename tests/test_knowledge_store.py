"""Knowledge file library tests — indexing, manual entries, retrieval."""

from __future__ import annotations

from pathlib import Path

import pytest

from coworker.knowledge.store import KnowledgeStore, _chunk_text, _ngram_vector


@pytest.fixture()
def store(tmp_path: Path) -> KnowledgeStore:
    return KnowledgeStore(tmp_path / "knowledge.db")


def test_ngram_vector_handles_chinese():
    v1 = _ngram_vector("固态电池是新型储能技术")
    v2 = _ngram_vector("固态电池是新型储能技术")
    v3 = _ngram_vector("今天天气很好适合出门")
    assert v1 == v2
    assert sum(v1.values()) > 0
    # same text cosine == 1 (roughly), unrelated text much lower
    dot = sum(v1[k] * v2.get(k, 0) for k in v1)
    assert dot > 0.9
    dot3 = sum(v1[k] * v3.get(k, 0) for k in v1)
    assert dot3 < dot


def test_chunk_text_overlaps():
    text = "字" * 2000
    chunks = _chunk_text(text, size=600, overlap=120)
    assert len(chunks) >= 3
    assert all(0 < len(c) <= 600 for c in chunks)


def test_add_text_and_search(store: KnowledgeStore):
    store.add_text("固态电池", "固态电池以固态电解质替代液态电解液，能量密度更高、更安全。", workspace="ws")
    store.add_text("量子计算", "量子比特利用叠加与纠缠态进行并行计算。", workspace="ws")

    hits = store.search("固态电池是什么", k=2, workspace="ws")
    assert hits, "expected a hit for the battery query"
    assert hits[0]["title"] == "固态电池"

    hits2 = store.search("量子比特", k=2, workspace="ws")
    assert hits2[0]["title"] == "量子计算"


def test_index_file_and_scan(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "guide.md").write_text("安装步骤：双击安装包，选择目录，点击完成。", encoding="utf-8")
    (ws / "notes.txt").write_text("备忘：明天开会。", encoding="utf-8")
    (ws / "code.py").write_text("print(1)", encoding="utf-8")  # not indexed

    store = KnowledgeStore(tmp_path / "knowledge.db", workspace=str(ws))
    summary = store.scan_workspace(str(ws))
    assert summary["added"] == 2, summary

    items = store.list_items(workspace=str(ws))
    assert len(items) == 2

    hits = store.search("安装步骤", k=1, workspace=str(ws))
    assert hits and hits[0]["source_path"].endswith("guide.md")

    # re-scan is a no-op (fingerprint unchanged)
    summary2 = store.scan_workspace(str(ws))
    assert summary2["added"] == 0

    # edit the file -> re-indexed (counted as updated, not added)
    (ws / "docs" / "guide.md").write_text("安装步骤：全新内容。", encoding="utf-8")
    summary3 = store.scan_workspace(str(ws))
    assert summary3["added"] == 0
    assert summary3["updated"] == 1


def test_scan_without_workspace_aggregates_all_indexed_dirs(tmp_path: Path):
    """问题1: scan_workspace() with NO workspace arg (no default set) must scan
    every already-indexed directory — the old code returned 0 added silently."""
    store = KnowledgeStore(tmp_path / "kb.db", workspace=None)

    ws_a = tmp_path / "proj-a"
    ws_a.mkdir(parents=True)
    (ws_a / "doc1.md").write_text("alpha beta gamma", encoding="utf-8")
    store.index_file(ws_a / "doc1.md", workspace=str(ws_a))

    # no workspace given -> should still scan proj-a (from its indexed source_path)
    res = store.scan_workspace()
    assert res.get("workspaces_scanned", 0) >= 1, res
    assert res["added"] == 0  # doc1 unchanged

    # a NEW doc in the same indexed dir is picked up
    (ws_a / "doc2.md").write_text("delta epsilon", encoding="utf-8")
    res2 = store.scan_workspace()
    assert res2["added"] == 1, res2

    # a second indexed workspace is scanned too
    ws_b = tmp_path / "proj-b"
    ws_b.mkdir(parents=True)
    (ws_b / "doc3.md").write_text("zeta eta", encoding="utf-8")
    store.index_file(ws_b / "doc3.md", workspace=str(ws_b))
    (ws_b / "doc4.md").write_text("theta iota", encoding="utf-8")
    res3 = store.scan_workspace()
    assert res3["added"] == 1, res3
    assert res3.get("workspaces_scanned", 0) >= 2, res3


def test_get_item_meta(tmp_path: Path):
    """问题3: get_item_meta returns the metadata a HORNET hit needs to link
    back to its source knowledge entry."""
    store = KnowledgeStore(tmp_path / "kb.db", workspace="/tmp/ws")
    item_id = store.add_text("标题A", "正文内容", workspace="/tmp/ws")
    meta = store.get_item_meta(item_id)
    assert meta is not None
    assert meta["title"] == "标题A"
    assert meta["kind"] == "manual"
    assert "workspace" in meta
    assert store.get_item_meta(999999) is None


def test_delete_item(store: KnowledgeStore):
    item_id = store.add_text("标题", "内容", workspace="ws")
    assert store.list_items(workspace="ws")
    assert store.delete(item_id) is True
    assert not store.list_items(workspace="ws")
    assert store.delete(item_id) is False


def test_embedder_injection(tmp_path: Path):
    calls: list[str] = []

    def fake_embedder(text: str) -> list[float]:
        calls.append(text)
        # crude: first char ordinal as a 1-d vector
        return [float(ord(text[0])) if text else 0.0]

    store = KnowledgeStore(tmp_path / "k.db", embedder=fake_embedder)
    store.add_text("A", "alpha beta", workspace="ws")
    store.search("alpha", k=1, workspace="ws")
    assert calls, "embedder should be used when injected"


def test_search_dedups_by_item_and_min_score(store: KnowledgeStore):
    # one long doc → several chunks; a second doc with a matching phrase
    store.add_text("多段文档", "固态电池是新型储能技术。" + "补充内容。" * 300, workspace="ws")
    store.add_text("另一篇", "固态电池在另一篇文档中的应用。", workspace="ws")

    hits = store.search("固态电池", k=3, workspace="ws")
    # top-k counts ITEMS, not chunks: both docs appear exactly once each
    item_ids = {h["item_id"] for h in hits}
    assert len(item_ids) == 2, hits
    assert len(hits) == 2

    # min_score filters out weak matches
    none = store.search("毫无关联的查询词xyz", k=5, workspace="ws", min_score=0.9)
    assert none == []


def test_add_text_rejects_empty(store: KnowledgeStore):
    with pytest.raises(ValueError):
        store.add_text("", "内容", workspace="ws")
    with pytest.raises(ValueError):
        store.add_text("标题", "", workspace="ws")


def test_agent_knowledge_tool_reads_unified_db(tmp_path: Path):
    """The agent's knowledge_search must read the SAME db the API/UI writes —
    regression for the UI/agent 'two libraries' split."""
    from coworker.agent import build_engine
    from coworker.agents import cowork_agent
    from coworker.knowledge import resolve_knowledge_db_path
    from coworker.providers import ModelCapabilities

    class _Stub:
        def complete(self, **kwargs):  # pragma: no cover - not invoked
            raise NotImplementedError

        def capabilities(self, model):
            return ModelCapabilities()

    ws = tmp_path / "ws"
    ws.mkdir()
    db = resolve_knowledge_db_path(workspace=str(ws))
    KnowledgeStore(db, workspace=str(ws)).add_text(
        "测试条目", "固态电池是新型储能技术。", workspace=str(ws)
    )

    engine = build_engine(agent=cowork_agent(), workspace=ws, provider=_Stub())
    try:
        result = engine.registry.execute("knowledge_search", {"query": "固态电池"})
        assert result["results"], result
        assert result["results"][0]["title"] == "测试条目"
    finally:
        engine.executor.close()


def test_index_folder_outside_workspace(tmp_path: Path):
    """A local folder OUTSIDE the workspace can be imported into the knowledge
    library; re-importing is idempotent (fingerprint dedup)."""
    external = tmp_path / "external-docs"
    (external / "sub").mkdir(parents=True)
    (external / "guide.md").write_text("固态电池导入说明：外部文件夹。", encoding="utf-8")
    (external / "sub" / "notes.txt").write_text("量子计算导入备忘。", encoding="utf-8")
    (external / "skip.xyz").write_text("not indexed", encoding="utf-8")

    ws = tmp_path / "ws"
    ws.mkdir()
    store = KnowledgeStore(tmp_path / "k.db", workspace=str(ws))

    first = store.index_folder(external, workspace=str(ws))
    assert first["added"] == 2, first
    assert first["failed"] == 0

    # items carry the external source paths
    paths = {it["source_path"] for it in store.list_items(workspace=str(ws))}
    assert any(p.endswith("guide.md") for p in paths)

    # searchable
    hits = store.search("固态电池", k=1, workspace=str(ws))
    assert hits and "外部文件夹" in hits[0]["content"]

    # idempotent re-import
    second = store.index_folder(external, workspace=str(ws))
    assert second["added"] == 0

    # deleting the original file does NOT break retrieval (content already stored)
    (external / "guide.md").unlink()
    hits2 = store.search("固态电池", k=1, workspace=str(ws))
    assert hits2 and "外部文件夹" in hits2[0]["content"]


def test_list_items_pagination_and_total(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    store = KnowledgeStore(tmp_path / "k.db", workspace=str(ws))
    for i in range(250):
        store.add_text(f"条目{i}", f"内容 {i} 号", workspace=str(ws))

    assert store.count_items(workspace=str(ws)) == 250
    page1 = store.list_items(workspace=str(ws), limit=100, offset=0)
    page2 = store.list_items(workspace=str(ws), limit=100, offset=100)
    page3 = store.list_items(workspace=str(ws), limit=100, offset=200)
    assert len(page1) == 100 and len(page2) == 100 and len(page3) == 50
    ids = {it["id"] for it in page1 + page2 + page3}
    assert len(ids) == 250  # no overlap, all 250 reachable via paging


def test_hornet_resonate_attaches_kb_metadata(tmp_path, monkeypatch):
    """问题3: manager.hornet_resonate must attach the source knowledge metadata
    (kb_item_id / source_path / kb_title) to every hit so the UI can offer
    'view source / research' like it does for emergent structures."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from coworker.hornet import HornetBuilder
    from coworker.server.manager import SessionManager

    mgr = SessionManager(data_dir=tmp_path / "data")
    kid = mgr.knowledge.add_text(
        "DPNN 相位记忆研究",
        "离散周期神经网络 皮萨诺周期 相位 共振 记忆",
        workspace="ws1",
    )
    HornetBuilder(mgr.hornet).build(
        [(kid, "DPNN 相位记忆研究", "离散周期神经网络 皮萨诺周期 相位 共振 记忆", 1700000000)]
    )
    r = mgr.hornet_resonate("DPNN 相位", k=3)
    assert r.get("hits"), "no resonance hits"
    for h in r["hits"]:
        assert "kb_item_id" in h
        assert h.get("kb_title") == "DPNN 相位记忆研究"
    # a deleted knowledge item degrades gracefully (no meta, no crash)
    mgr.knowledge.delete(kid)
    r2 = mgr.hornet_resonate("DPNN 相位", k=3)
    assert "hits" in r2


# -- S11 知识资产版本控制 (蜂群审计报告 G5) ------------------------------------

def test_index_file_update_snapshots_history(tmp_path):
    """重索引 (文件变化) 时旧内容快照进历史版本链, 版本递增。"""
    store = KnowledgeStore(tmp_path / "kb.db")
    f = tmp_path / "doc.md"
    f.write_text("第一版内容", encoding="utf-8")
    store.index_file(f, workspace="ws")
    item_id = store._con.execute(
        "SELECT id FROM knowledge_items WHERE source_path=?", (str(f),)
    ).fetchone()[0]

    # 文件变化 → 重索引
    import time

    time.sleep(0.01)
    f.write_text("第二版内容更长一些", encoding="utf-8")
    store.index_file(f, workspace="ws", force=True)

    hist = store.history(item_id)
    assert len(hist) >= 2
    assert hist[0]["current"] is True
    # 历史里有第一版内容
    assert any(h["content"] == "第一版内容" for h in hist)
    store.close()


def test_knowledge_rollback_restores_old_content(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    f = tmp_path / "doc.md"
    f.write_text("第一版内容", encoding="utf-8")
    store.index_file(f, workspace="ws")
    item_id = store._con.execute(
        "SELECT id FROM knowledge_items WHERE source_path=?", (str(f),)
    ).fetchone()[0]

    import time

    time.sleep(0.01)
    f.write_text("第二版内容更长一些", encoding="utf-8")
    store.index_file(f, workspace="ws", force=True)
    # 回滚到 v1
    ok = store.rollback(item_id, 1)
    assert ok is True
    # 回滚后内容恢复第一版
    chunks = store._con.execute(
        "SELECT content FROM knowledge_chunks WHERE item_id=? ORDER BY chunk_index",
        (item_id,),
    ).fetchall()
    assert any("第一版内容" in c[0] for c in chunks)
    store.close()


def test_knowledge_rollback_unknown_version(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    f = tmp_path / "doc.md"
    f.write_text("内容", encoding="utf-8")
    store.index_file(f, workspace="ws")
    item_id = store._con.execute(
        "SELECT id FROM knowledge_items WHERE source_path=?", (str(f),)
    ).fetchone()[0]
    assert store.rollback(item_id, 99) is False  # 不存在版本, 不改动
    store.close()


# -- S5 知识去重 --------------------------------------------------------------

def test_knowledge_dedupe_retires_duplicates(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    # 同 title + 同 content 的两条 (不同 source_path, 绕过 UNIQUE)
    a = store.add_text("重复知识", "完全相同的内容正文", workspace="ws1")
    b = store.add_text("重复知识", "完全相同的内容正文", workspace="ws2")
    result = store.dedupe()
    assert b in result["retired"]  # 后插入的 retired
    # retired 后检索不可见但保留
    hits = store.search("完全相同的内容正文", workspace="ws2", k=5)
    assert not any(h.get("item_id") == b for h in hits)
    store.close()


def test_knowledge_dedupe_dry_run(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    a = store.add_text("重复知识", "完全相同的内容正文", workspace="ws1")
    b = store.add_text("重复知识", "完全相同的内容正文", workspace="ws2")
    result = store.dedupe(dry_run=True)
    assert result["dry_run"] is True
    assert b in result["retired"]
    # 未实际 retired
    row = store._con.execute(
        "SELECT retired FROM knowledge_items WHERE id=?", (b,)
    ).fetchone()
    assert row[0] == 0
    store.close()


# -- M0 访问日志 (黄金衡分形存储 P1 前置) --------------------------------------


def test_access_log_records_search_hits(tmp_path):
    """检索命中 → access_log 写入时间序列 + use_count 累计。"""
    store = KnowledgeStore(tmp_path / "kb.db")
    item_id = store.add_text("固态电池", "固态电池以固态电解质替代液态电解液。", workspace="ws")
    hits = store.search("固态电池", workspace="ws", k=3)
    assert hits and hits[0]["item_id"] == item_id
    # 窗口模式: 命中项频率 ≥ 1 次/小时 (刚发生)
    freq = store.access_frequency(window_hours=1.0)
    assert freq.get(item_id, 0) >= 1.0
    # 累计模式: use_count 代理
    cum = store.access_frequency()
    assert cum.get(item_id) == 1.0
    store.close()


def test_access_log_disabled(tmp_path):
    """access_log=False → 不写时间序列 (开关可关)。"""
    store = KnowledgeStore(tmp_path / "kb.db", access_log=False)
    item_id = store.add_text("固态电池", "固态电池以固态电解质替代液态电解液。", workspace="ws")
    hits = store.search("固态电池", workspace="ws", k=3)
    assert hits
    n = store._con.execute("SELECT COUNT(*) FROM knowledge_access_log").fetchone()[0]
    assert n == 0
    # use_count 照常累计 (原资产生命周期不受影响)
    assert store.access_frequency().get(item_id) == 1.0
    store.close()


def test_access_log_prune_both_dimensions(tmp_path):
    """双维度清理: 天数窗口删老记录, 条数窗口保最新 N 条。"""
    import time as _time

    store = KnowledgeStore(tmp_path / "kb.db")
    # 注入一条 40 天前的老记录 (超 30 天窗口)
    old_ts = _time.time() - 40 * 86400
    store._con.execute(
        "INSERT INTO knowledge_access_log (item_id, action, ts) VALUES (1, 'search', ?)",
        (old_ts,),
    )
    # 注入 keep_count+5 条近期记录 (条数窗口只保最新 keep_count)
    now = _time.time()
    keep_count = 10
    for i in range(keep_count + 5):
        store._con.execute(
            "INSERT INTO knowledge_access_log (item_id, action, ts) VALUES (2, 'search', ?)",
            (now - i, ),
        )
    store._con.commit()
    result = store.prune_access_log(keep_days=30, keep_count=keep_count)
    assert result["keep_days"] == 30 and result["keep_count"] == keep_count
    assert result["removed"] == 6  # 1 老记录 + 5 超出条数窗口
    assert result["kept"] == keep_count
    # 剩余全部是最新记录
    remaining = store._con.execute(
        "SELECT COUNT(*) FROM knowledge_access_log WHERE item_id=2"
    ).fetchone()[0]
    assert remaining == keep_count
    store.close()


def test_access_frequency_window_empty(tmp_path):
    """无命中时窗口模式返回空 dict, 不抛错。"""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_text("量子计算", "量子比特利用叠加与纠缠态。", workspace="ws")
    assert store.access_frequency(window_hours=1.0) == {}
    assert store.access_frequency() == {}
    store.close()

