"""run_store maintenance: orphaned-run reaping + event-stream pruning.

Both are boot-time housekeeping added to close the tech-debt where (a) a sidecar
restart left a swarm run's DB row stuck at status='running' (the GUI then spun a
run that could never finish) and (b) orchestration_events grew unbounded because
each run streams hundreds of worker_thought / decision_trace rows that are never
deleted. These mirror automation's `reap_stale_runs`.
"""

from __future__ import annotations

import time

from coworker.orchestrator.run_store import OrchestrationRunStore


def _age_run(store, run_id, seconds):
    """Backdate a run's heartbeat (updated_at) to simulate a silent/dead run."""
    store._db.execute(  # direct: the public API always writes now()
        "UPDATE orchestration_runs SET updated_at = ? WHERE run_id = ?",
        (time.time() - seconds, run_id),
    )
    store._db.commit()


def _age_created(store, run_id, seconds):
    """Backdate created_at so the run sorts oldest (prune ordering is by created_at)."""
    store._db.execute(
        "UPDATE orchestration_runs SET created_at = ? WHERE run_id = ?",
        (time.time() - seconds, run_id),
    )
    store._db.commit()


def test_reap_orphaned_runs_flips_stale_running_to_failed(tmp_path):
    store = OrchestrationRunStore(tmp_path / "orch.db")
    dead = store.create_run("dead goal")          # created as 'running'
    _age_run(store, dead, 600)                    # heartbeat 10 min old
    assert store.reap_orphaned_runs(older_than=120) == 1
    run = store.get_run(dead)
    assert run["status"] == "failed"
    assert "重启" in (run["final"] or "")          # an honest note, not silent data loss
    store.close()


def test_reap_spares_fresh_and_active_runs(tmp_path):
    store = OrchestrationRunStore(tmp_path / "orch.db")
    fresh = store.create_run("fresh")             # running, heartbeat now → within window
    live_orphan = store.create_run("live")
    _age_run(store, live_orphan, 600)             # old heartbeat, BUT in active set
    n = store.reap_orphaned_runs(older_than=120, active_run_ids={live_orphan})
    assert n == 0
    assert store.get_run(fresh)["status"] == "running"
    assert store.get_run(live_orphan)["status"] == "running"  # spared by active guard
    store.close()


def test_reap_ignores_terminal_runs(tmp_path):
    store = OrchestrationRunStore(tmp_path / "orch.db")
    done = store.create_run("done")
    store.update_status(done, "completed")
    _age_run(store, done, 999)
    assert store.reap_orphaned_runs(older_than=120) == 0
    assert store.get_run(done)["status"] == "completed"
    store.close()


def test_prune_events_keeps_recent_and_running_drops_old(tmp_path):
    store = OrchestrationRunStore(tmp_path / "orch.db")
    # 3 old finished runs, each with events; keep_runs=1 → only the newest survives.
    old_ids = []
    for i in range(3):
        rid = store.create_run(f"old {i}")
        for k in range(5):
            store.append_event(rid, "worker_thought", {"i": k})
        store.update_status(rid, "completed")
        old_ids.append(rid)
        time.sleep(0.01)  # ensure distinct created_at ordering
    # a running run whose created_at is the OLDEST must still be spared (status branch).
    live = store.create_run("live")
    for k in range(4):
        store.append_event(live, "worker_thought", {"i": k})
    _age_created(store, live, 9999)  # sorts oldest, but status='running' saves it

    deleted = store.prune_events(keep_runs=1)
    # keep = newest-1 (old2) ∪ running (live). The two older finished runs' events go
    # (old0 + old1 = 10 rows); newest finished + the running run stay.
    assert deleted == 10
    assert store.get_run(old_ids[2])["events"] != []   # newest finished kept
    assert store.get_run(live)["events"] != []          # running kept regardless of age
    assert store.get_run(old_ids[0])["events"] == []   # oldest finished pruned
    store.close()


def test_prune_events_is_safe_on_empty_store(tmp_path):
    store = OrchestrationRunStore(tmp_path / "orch.db")
    assert store.prune_events(keep_runs=100) == 0
    store.close()


# -- read-only cross-store aggregation (双 orchestration.db) -------------------
import sqlite3

from coworker.orchestrator.run_store import read_only_get_run, read_only_list_runs


def test_read_only_list_runs_and_get_run(tmp_path):
    src = OrchestrationRunStore(tmp_path / "orchestration.db")
    rid = src.create_run("chat-tool goal")
    src.append_event(rid, "worker_thought", {"text": "hi"})
    src.update_status(rid, "completed", final="the deliverable")
    src.close()

    db = tmp_path / "orchestration.db"
    rows = read_only_list_runs(db, limit=50)
    assert len(rows) == 1 and rows[0]["run_id"] == rid
    assert rows[0]["status"] == "completed"
    run = read_only_get_run(db, rid)
    assert run and run["final"] == "the deliverable"
    assert any(e["kind"] == "worker_thought" for e in run["events"])


def test_read_only_list_runs_never_creates_a_missing_db(tmp_path):
    # THE safety property: scanning a workspace with no store must NOT create one
    # (OrchestrationRunStore.__init__ would; the read-only helper must not).
    ghost = tmp_path / "nope" / "orchestration.db"
    assert read_only_list_runs(ghost, limit=50) == []
    assert read_only_get_run(ghost, "orch_x") is None
    assert not ghost.exists()
    assert not (tmp_path / "nope").exists()


def test_read_only_handles_non_orchestration_db(tmp_path):
    # An existing sqlite file without the runs table → [], None (never raises).
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE z(x)")
    conn.commit()
    conn.close()
    assert read_only_list_runs(other) == []
    assert read_only_get_run(other, "orch_x") is None


def test_read_only_does_not_write(tmp_path):
    src = OrchestrationRunStore(tmp_path / "orchestration.db")
    src.create_run("x")
    src.close()
    db = tmp_path / "orchestration.db"
    before = db.read_bytes()
    read_only_list_runs(db, limit=50)
    read_only_get_run(db, "orch_whatever")
    assert db.read_bytes() == before  # mode=ro: byte-identical after reads


def test_manager_merges_panel_and_workspace_history(tmp_path):
    """GUI history must list chat-tool swarm runs (per-workspace .qunwork/ store)
    alongside panel runs, and detail/report must resolve across both stores — read-only."""
    from coworker.server.manager import SessionManager

    ws = tmp_path / "proj"
    ws.mkdir()
    mgr = SessionManager(data_dir=tmp_path / "data", workspace=str(ws))
    mgr.default_workspace = str(ws)

    panel_run = mgr.orchestration_store.create_run("panel goal")
    mgr.orchestration_store.update_status(panel_run, "completed")

    chat_db = ws / ".qunwork" / "orchestration.db"
    chat_store = OrchestrationRunStore(chat_db)
    chat_run = chat_store.create_run("chat goal")
    chat_store.update_status(chat_run, "completed", final="chat deliverable")
    chat_store.close()

    hist = mgr.orchestration_history_merged(limit=50)
    srcs = {h["run_id"]: h.get("source") for h in hist}
    assert srcs.get(panel_run) == "panel"
    assert srcs.get(chat_run) == "workspace"  # was INVISIBLE before this change
    assert chat_run not in {r["run_id"] for r in mgr.orchestration_store.list_runs(limit=50)}

    # detail resolves the chat-tool run across stores (else history rows would 404)
    got = mgr.orchestration_get_run(chat_run)
    assert got and got["final"] == "chat deliverable"
    assert mgr.orchestration_get_run("orch_does_not_exist") is None
