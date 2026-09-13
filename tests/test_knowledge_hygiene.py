"""Knowledge-library hygiene: list/count agreement, scan exclusions, hash backfill and
the cross-workspace duplicate rule.

Origin (owner-hit 2026-09-13): the page claimed "200/21782 entries" while only 8,944 rows
could ever load — `count_items` counted retired (superseded) rows that `list_items` hides.
The same investigation turned up 2,713 files indexed once per nested workspace root
(4,897 redundant rows, 96% of all chunks) and content_hash empty on every legacy row.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coworker.knowledge.purge import cross_workspace_duplicates, purge_retired_chunks
from coworker.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path: Path) -> KnowledgeStore:
    return KnowledgeStore(tmp_path / "knowledge.db")


# -- ① list and count must agree -------------------------------------------------------------


def test_count_items_hides_retired_rows_like_the_list_does(store: KnowledgeStore):
    a = store.add_text("keep me", "内容 A", workspace="ws")
    b = store.add_text("old version", "内容 B", workspace="ws")

    assert store.count_items(workspace="ws") == 2
    assert len(store.list_items(workspace="ws")) == 2

    store.set_retired(b, True)
    # The list drops it; the count must too (it did not, and the UI lied by 2.4×).
    assert len(store.list_items(workspace="ws")) == 1
    assert store.count_items(workspace="ws") == 1
    # Audit callers can still ask for the full number.
    assert store.count_items(workspace="ws", include_retired=True) == 2
    assert a  # silence unused warnings


def test_count_items_across_all_workspaces_matches_the_unscoped_list(store: KnowledgeStore):
    store.add_text("one", "内容 1", workspace="ws-a")
    two = store.add_text("two", "内容 2", workspace="ws-b")
    store.set_retired(two, True)

    assert len(store.list_items()) == 1
    assert store.count_items() == 1
    assert store.count_items(include_retired=True) == 2


# -- ⑤ scan exclusions -----------------------------------------------------------------------


def test_scan_skips_the_packaged_python_runtime(store: KnowledgeStore, tmp_path: Path):
    """PyInstaller's `_internal` tree is thousands of license/metadata texts — noise that
    used to be indexed (and re-indexed per build)."""
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "real.md").write_text("真实文档内容", encoding="utf-8")
    sidecar = ws / "src-tauri" / "binaries" / "sidecar" / "_internal" / "numpy" / "licenses"
    sidecar.mkdir(parents=True)
    (sidecar / "LICENSE.txt").write_text("license text that is not knowledge", encoding="utf-8")

    store.scan_workspace(str(ws))
    paths = {it["source_path"] for it in store.list_items(workspace=str(ws))}
    assert any("real.md" in p for p in paths)
    assert not any("_internal" in p for p in paths)


# -- ④ content-hash backfill -----------------------------------------------------------------


def test_backfill_fills_hashes_from_disk_and_from_chunks(store: KnowledgeStore, tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    doc = ws / "doc.md"
    doc.write_text("磁盘上的文档内容", encoding="utf-8")
    item_id = store.index_file(doc, workspace=str(ws))
    assert item_id

    # Simulate a pre-2026-09-06 row: hash missing.
    store._con.execute("UPDATE knowledge_items SET content_hash='' WHERE id=?", (item_id,))
    store._con.commit()

    report = store.backfill_content_hashes()
    assert report["filled"] == 1
    assert report["from_disk"] == 1
    row = store._con.execute(
        "SELECT content_hash FROM knowledge_items WHERE id=?", (item_id,)
    ).fetchone()
    assert row[0], "the hash must be filled so content dedupe can see this row"


def test_backfill_falls_back_to_stored_chunks_when_the_source_is_gone(store: KnowledgeStore, tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    doc = ws / "gone.md"
    doc.write_text("这段内容之后源文件会被删掉", encoding="utf-8")
    item_id = store.index_file(doc, workspace=str(ws))
    doc.unlink()

    store._con.execute("UPDATE knowledge_items SET content_hash=NULL WHERE id=?", (item_id,))
    store._con.commit()

    report = store.backfill_content_hashes()
    assert report["filled"] == 1
    assert report["from_chunks"] == 1


def test_backfill_active_only_skips_retired_rows(store: KnowledgeStore, tmp_path: Path):
    """Retired rows are invisible to search and dedupe and their chunks are what the purge
    deletes — hashing 12.8k of them made a 2-minute job out of a 20-minute one."""
    ws = tmp_path / "ws"
    ws.mkdir()
    live = ws / "live.md"
    live.write_text("现役文档", encoding="utf-8")
    dead = ws / "dead.md"
    dead.write_text("退役文档", encoding="utf-8")
    live_id = store.index_file(live, workspace=str(ws))
    dead_id = store.index_file(dead, workspace=str(ws))
    store.set_retired(dead_id, True)
    store._con.execute("UPDATE knowledge_items SET content_hash=''")
    store._con.commit()

    report = store.backfill_content_hashes(active_only=True, commit_every=1)
    assert report["candidates"] == 1 and report["filled"] == 1
    assert report["skipped_retired"] == 1
    rows = dict(store._con.execute("SELECT id, content_hash FROM knowledge_items").fetchall())
    assert rows[live_id] and not rows[dead_id]


def test_backfill_batching_loses_no_rows(store: KnowledgeStore, tmp_path: Path):
    """commit_every=N must not skip the tail (last <N rows) nor stop early."""
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(7):
        doc = ws / f"d{i}.md"
        doc.write_text(f"文档 {i} 内容", encoding="utf-8")
        store.index_file(doc, workspace=str(ws))
    store._con.execute("UPDATE knowledge_items SET content_hash=''")
    store._con.commit()

    report = store.backfill_content_hashes(commit_every=3)
    assert report["filled"] == 7
    missing = store._con.execute(
        "SELECT COUNT(*) FROM knowledge_items WHERE content_hash IS NULL OR content_hash=''"
    ).fetchone()[0]
    assert missing == 0


def test_backfill_limit_slices_the_work(store: KnowledgeStore, tmp_path: Path):
    """Re-extracting a source is expensive (a big PDF costs seconds), so the CLI lets the
    operator run the backfill in slices; `limit` must cap the batch exactly."""
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(5):
        doc = ws / f"d{i}.md"
        doc.write_text(f"文档 {i}", encoding="utf-8")
        store.index_file(doc, workspace=str(ws))
    store._con.execute("UPDATE knowledge_items SET content_hash=''")
    store._con.commit()

    first = store.backfill_content_hashes(limit=2)
    assert first["candidates"] == 2 and first["filled"] == 2
    left = store._con.execute(
        "SELECT COUNT(*) FROM knowledge_items WHERE content_hash IS NULL OR content_hash=''"
    ).fetchone()[0]
    assert left == 3
    assert store.backfill_content_hashes()["filled"] == 3


# -- ③ cross-workspace duplicate rule --------------------------------------------------------


def _seed_copies(store: KnowledgeStore, file: Path, workspaces: list[str]) -> list[int]:
    """One item per workspace for the same file, mimicking nested scan roots."""
    ids = []
    for ws in workspaces:
        cur = store._con.execute(
            "INSERT INTO knowledge_items (workspace, kind, source_path, title, fingerprint, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (ws, "file", str(file), file.stem, "fp", 1.0, 1.0),
        )
        ids.append(cur.lastrowid)
    store._con.commit()
    return ids


def test_cross_workspace_rule_keeps_the_deepest_root_and_retires_the_rest(
    store: KnowledgeStore, tmp_path: Path
):
    root = tmp_path / "QunWork"
    deep = root / "QunWork" / "docs"
    deep.mkdir(parents=True)
    doc = deep / "guide.md"
    doc.write_text("内容足够长以便切块" * 40, encoding="utf-8")

    # The shallow copies carry the chunks (they were indexed first); the deepest root is
    # the semantically right home and must win anyway.
    shallow = _seed_copies(store, doc, [str(tmp_path), str(root)])
    for item_id in shallow:
        store._index_chunks(item_id, doc.stem, doc.read_text(encoding="utf-8"))
    keep = _seed_copies(store, doc, [str(root / "QunWork")])[0]
    store._index_chunks(keep, doc.stem, doc.read_text(encoding="utf-8"))
    store._con.commit()

    plan, retire_ids = cross_workspace_duplicates(store._con)
    assert len(plan) == 1
    assert plan[0]["keep"]["id"] == keep
    assert sorted(retire_ids) == sorted(shallow)


def test_cross_workspace_rule_refuses_to_retire_when_no_copy_has_content(
    store: KnowledgeStore, tmp_path: Path
):
    doc = tmp_path / "QunWork" / "empty.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("x", encoding="utf-8")
    _seed_copies(store, doc, [str(tmp_path), str(tmp_path / "QunWork")])

    plan, retire_ids = cross_workspace_duplicates(store._con)
    assert plan == []
    assert retire_ids == [], "retiring every copy would destroy the only content"


def test_cross_workspace_rule_ignores_single_workspace_paths(store: KnowledgeStore, tmp_path: Path):
    doc = tmp_path / "solo.md"
    doc.write_text("x", encoding="utf-8")
    _seed_copies(store, doc, [str(tmp_path)])
    plan, retire_ids = cross_workspace_duplicates(store._con)
    assert plan == [] and retire_ids == []


def test_cross_workspace_rule_output_is_json_safe(store: KnowledgeStore, tmp_path: Path):
    """The purge CLI serialises the plan — it must survive json.dumps."""
    import json

    doc = tmp_path / "QunWork" / "j.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("y" * 200, encoding="utf-8")
    ids = _seed_copies(store, doc, [str(tmp_path), str(tmp_path / "QunWork")])
    for i in ids:
        store._index_chunks(i, "j", "y" * 200)
    store._con.commit()
    plan, _ = cross_workspace_duplicates(store._con)
    json.dumps(plan)  # raises if the plan holds sqlite3.Row / Path objects


# -- chunk purge batching --------------------------------------------------------------------


def test_purge_retired_chunks_deletes_only_retired_and_survives_batching(
    store: KnowledgeStore, tmp_path: Path
):
    """The 6 GB library runs with journal_mode=delete, so the purge commits every few rows;
    the loop must reach the end (rowcount < batch) without dropping or over-deleting rows."""
    keep = store.add_text("keep", "保留内容" * 30, workspace="ws")
    gone = store.add_text("gone", "待删内容" * 30, workspace="ws")
    store._index_chunks(keep, "keep", "保留内容" * 30)
    store._index_chunks(gone, "gone", "待删内容" * 30)
    store._con.commit()
    assert store._con.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0] > 2

    store.set_retired(gone, True)
    deleted = purge_retired_chunks(store._con, batch=1)

    gone_chunks = store._con.execute(
        "SELECT COUNT(*) FROM knowledge_chunks WHERE item_id=?", (gone,)
    ).fetchone()[0]
    assert gone_chunks == 0
    assert deleted >= 1
    assert (
        store._con.execute(
            "SELECT COUNT(*) FROM knowledge_chunks WHERE item_id=?", (keep,)
        ).fetchone()[0]
        > 0
    ), "an active item must keep its content"


def test_purge_retired_chunks_is_a_noop_when_nothing_is_retired(store: KnowledgeStore):
    item = store.add_text("live", "内容" * 30, workspace="ws")
    store._index_chunks(item, "live", "内容" * 30)
    store._con.commit()
    before = store._con.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
    assert purge_retired_chunks(store._con, batch=1) == 0
    assert store._con.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0] == before
