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

    return TestClient(create_app(manager))


def test_orchestrate_returns_task_dag(client):
    r = client.post("/v1/orchestrate", json={"intent": "Write a report"})
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
