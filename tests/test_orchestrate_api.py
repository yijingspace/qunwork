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
            AssistantTurn(text="report draft", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}'),
            AssistantTurn(text="review notes", finish_reason="stop"),
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
