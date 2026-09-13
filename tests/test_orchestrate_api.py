"""POST /v1/orchestrate API tests."""

import pytest

from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient


class ScriptedProvider(ProviderClient):
    def __init__(self, turns):
        self._turns = list(turns)

    def complete(self, *, model, messages, tools=None, **settings):
        assert self._turns, "no scripted turn left"
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    from coworker.server.manager import SessionManager

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = ScriptedProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Write a report","deps":[]},'
                                 '{"id":"t1","description":"Review it","deps":["t0"]}]'),
            AssistantTurn(text="report draft " + "x" * 110, finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}'),
            AssistantTurn(text="review notes " + "x" * 110, finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.95,"reason":"ok","needs_human":false}'),
        ]
    )
    return SessionManager(data_dir=tmp_path / "data", provider=provider, workspace=str(ws))


@pytest.fixture()
def client(manager):
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app

    with TestClient(create_app(manager)) as c:
        yield c


def test_orchestrate_returns_task_dag(client):
    r = client.post("/v1/orchestrate", json={"intent": "Write a report", "sync": True})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["status"] == "completed"
    tasks = data["tasks"]
    assert [t["id"] for t in tasks] == ["t0", "t1"]
    assert tasks[0]["status"] == "done"
    assert tasks[1]["deps"] == ["t0"]
    assert "report draft" in tasks[0]["result"]
    assert data["session_id"].startswith("__orchestrate__")


def test_orchestrate_requires_intent(client):
    r = client.post("/v1/orchestrate", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "intent" in r.json()["error"]


def test_orchestrate_report_redacted_export(client):
    """`?redact=1` returns the PUBLISHABLE copy (masking rules themselves are unit-tested in
    test_coordination_report.py): flagged, written under its own filename, note prepended."""
    client.post("/v1/orchestrate", json={"intent": "Write a report", "sync": True})
    run_id = client.get("/v1/orchestrate/history").json()["runs"][0]["run_id"]

    raw = client.get(f"/v1/orchestrate/{run_id}/report").json()
    assert raw["ok"] is True
    assert raw["redacted"] is False
    assert "已脱敏" not in raw["markdown"]
    assert raw["report_path"].endswith(f"coordination-report-{run_id}.md")

    red = client.get(f"/v1/orchestrate/{run_id}/report?redact=1").json()
    assert red["ok"] is True
    assert red["redacted"] is True
    assert red["markdown"].startswith("> 本样例已脱敏")
    # Its own file: the raw report is never overwritten by the sanitized copy.
    assert red["report_path"].endswith(f"coordination-report-{run_id}-redacted.md")


def test_orchestrate_async_poll_and_history(tmp_path, monkeypatch):
    """Async POST returns a run_id; progress events + final state are pollable."""
    from coworker.server.manager import SessionManager

    from coworker.server.app import create_app

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = ScriptedProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Write a report","deps":[]}]'),
            AssistantTurn(text="thinking about the outline… report draft", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}'),
        ]
    )
    manager = SessionManager(data_dir=tmp_path / "data", provider=provider, workspace=str(ws))
    from fastapi.testclient import TestClient

    with TestClient(create_app(manager)) as client:
        # async start
        r = client.post("/v1/orchestrate", json={"intent": "Write a report"})
        data = r.json()
        assert data["ok"] and data.get("async") is True
        run_id = data["run_id"]

        # poll until completed (background task runs on the event loop via create_task)
        import time

        snap = None
        for _ in range(20):
            snap = client.get(f"/v1/orchestrate/{run_id}").json()
            if snap["ok"] and snap["status"] != "running":
                break
            time.sleep(0.2)
        assert snap["ok"] and snap["status"] == "completed"
        kinds = [e["kind"] for e in snap["events"]]
        assert "run_started" in kinds and "plan_ready" in kinds and "run_completed" in kinds
        # chain-of-thought: the executor's intermediate message was streamed.
        thoughts = [e for e in snap["events"] if e["kind"] == "worker_thought"]
        assert any("thinking about the outline" in t["payload"]["text"] for t in thoughts)

        # history lists the run
        hist = client.get("/v1/orchestrate/history").json()
        assert any(h["run_id"] == run_id for h in hist["runs"])


def test_orchestrate_run_not_found(client):
    r = client.get("/v1/orchestrate/does-not-exist")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_orchestrate_accepts_timeout_and_max_parallel(tmp_path, monkeypatch):
    """The panel config (max_parallel / timeout_seconds) flows through the API."""
    import time as _time

    from coworker.server.manager import SessionManager

    from coworker.server.app import create_app

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()

    class P(ProviderClient):
        def __init__(self):
            self.turns = [
                AssistantTurn(text='[{"id":"t0","description":"Write a report","deps":[]}]'),
                AssistantTurn(text="done", finish_reason="stop"),
                AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}'),
            ]

        def complete(self, *, model, messages, tools=None, **settings):
            assert self.turns
            return self.turns.pop(0)

        def capabilities(self, model):
            return ModelCapabilities()

    manager = SessionManager(data_dir=tmp_path / "data", provider=P(), workspace=str(ws))
    from fastapi.testclient import TestClient

    with TestClient(create_app(manager)) as client:
        r = client.post(
            "/v1/orchestrate",
            json={"intent": "Write a report", "sync": True, "max_parallel": 3, "timeout_seconds": 120},
        )
        assert r.json()["ok"] is True and r.json()["status"] == "completed"


def test_parse_swarm_timeout_semantics():
    """0 / 负数 / null → 两层超时全部 None (不限制); 缺省/正数 → 有限 + 单任务 240s。"""
    from coworker.server.app import _parse_swarm_timeout as p

    assert p({}) == (300, 240)                       # 缺省: 旧行为不变
    assert p({"timeout_seconds": 120}) == (120, 240)  # 有限整轮 + 默认单任务
    assert p({"timeout_seconds": "600"}) == (600, 240)  # 字符串数字 (GUI JSON 可能带引号)
    assert p({"timeout_seconds": 0}) == (None, None)   # 显式 0 = 不限制
    assert p({"timeout_seconds": None}) == (None, None)
    assert p({"timeout_seconds": -1}) == (None, None)  # 负数按不限制处理
    assert p({"timeout_seconds": "abc"}) == (300, 240)  # 非法值回退缺省


def test_orchestrate_no_timeout_runs_to_completion(tmp_path, monkeypatch):
    """timeout_seconds: 0 (不限制) 被端点接受并正常跑完 (旧代码会把 0 吞成 300)。"""
    from coworker.server.manager import SessionManager

    from coworker.server.app import create_app

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()

    class P(ProviderClient):
        def __init__(self):
            self.turns = [
                AssistantTurn(text='[{"id":"t0","description":"Write a report","deps":[]}]'),
                AssistantTurn(text="report draft " + "x" * 120, finish_reason="stop"),
                AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}'),
            ]

        def complete(self, *, model, messages, tools=None, **settings):
            assert self.turns
            return self.turns.pop(0)

        def capabilities(self, model):
            return ModelCapabilities()

    manager = SessionManager(data_dir=tmp_path / "data", provider=P(), workspace=str(ws))
    from fastapi.testclient import TestClient

    with TestClient(create_app(manager)) as client:
        r = client.post(
            "/v1/orchestrate",
            json={"intent": "Write a report", "sync": True, "timeout_seconds": 0},
        )
        assert r.json()["ok"] is True and r.json()["status"] == "completed"


def test_run_store_heartbeat_updates_updated_at(tmp_path):
    """Appending an event refreshes the run's updated_at (live vs orphaned)."""
    from coworker.orchestrator.run_store import OrchestrationRunStore

    store = OrchestrationRunStore(tmp_path / "orch.db")
    run_id = store.create_run("x")
    before = store.get_run(run_id)["updated_at"]
    import time as _time

    _time.sleep(0.01)
    store.append_event(run_id, "worker_thought", {"text": "thinking"})
    after = store.get_run(run_id)["updated_at"]
    assert after > before
    store.close()


# -- disk-full hardening (owner-hit 2026-09-13) -------------------------------------------


def test_orchestrate_refuses_to_start_on_an_unwritable_workspace(client, monkeypatch):
    """A full volume used to start anyway and ship a 0-byte deliverable."""
    import coworker.diskspace as ds

    monkeypatch.setattr(
        ds,
        "check_writable",
        lambda *_a, **_k: ds.SpaceReport(
            ok=False, reason="alloc-failed", free_bytes=52 * 1024**3,
            error="E:\\ cannot allocate new data (reports 52.0 GB free): disk full",
        ),
    )
    r = client.post("/v1/orchestrate", json={"intent": "Write a report", "sync": True})
    body = r.json()
    assert body["ok"] is False
    assert body["space"] == "alloc-failed"
    assert "cannot allocate" in body["error"]
    # Nothing was created — no ghost run in the history.
    assert client.get("/v1/orchestrate/history").json()["runs"] == []


def test_snapshot_reports_a_storage_failure_that_the_record_could_not_hold(client, manager):
    """The store is exactly what died, so the reason rides the READ from memory."""
    client.post("/v1/orchestrate", json={"intent": "Write a report", "sync": True})
    run_id = client.get("/v1/orchestrate/history").json()["runs"][0]["run_id"]

    manager.orchestration_storage_errors[run_id] = "OperationalError: database or disk is full"
    snap = client.get(f"/v1/orchestrate/{run_id}").json()
    assert snap["storage_error"] == "OperationalError: database or disk is full"

    # A healthy run carries no such annotation.
    manager.orchestration_storage_errors.pop(run_id)
    assert "storage_error" not in client.get(f"/v1/orchestrate/{run_id}").json()


def test_abandon_closes_a_frozen_running_run(client, manager):
    """A ghost run keeps status=running forever; the owner needs a way out."""
    client.post("/v1/orchestrate", json={"intent": "Write a report", "sync": True})
    store = manager.orchestration_store
    run_id = client.get("/v1/orchestrate/history").json()["runs"][0]["run_id"]
    store.update_status(run_id, "running")  # simulate the frozen-in-time record
    manager.orchestration_storage_errors[run_id] = "OSError: disk full"

    r = client.post(f"/v1/orchestrate/{run_id}/abandon").json()
    assert r["ok"] is True
    assert r["status"] == "failed"
    assert "disk full" in r["reason"]
    assert store.get_run(run_id)["status"] == "failed"
    assert "abandoned" in (store.get_run(run_id)["final"] or "")

    # Second call is a no-op (already closed), and the stale reason is cleared.
    again = client.post(f"/v1/orchestrate/{run_id}/abandon").json()
    assert again["ok"] is True and again.get("already_closed") is True
