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
    (external / "skip.log").write_text("not indexed", encoding="utf-8")

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
