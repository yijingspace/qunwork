"""Cross-session / cross-task persistent memory tests (Phase 4)."""

from coworker.orchestrator import Orchestrator, PersistentVectorMemory
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient


def _embedder(s: str) -> list[float]:
    return [
        1.0 if "report" in s else 0.0,
        1.0 if "code" in s else 0.0,
    ]


# -- store unit tests -------------------------------------------------------


def test_persistent_memory_roundtrip(tmp_path):
    db = tmp_path / "memory.db"
    m1 = PersistentVectorMemory(db, scope="ws1", embedder=_embedder)
    m1.add("Write a quarterly report", task_id="t0")
    m1.close()

    m2 = PersistentVectorMemory(db, scope="ws1", embedder=_embedder)  # fresh process
    assert len(m2) == 1
    hits = m2.search("Prepare a report", k=1)
    assert hits and hits[0].meta["task_id"] == "t0"
    m2.close()


def test_scope_isolation(tmp_path):
    db = tmp_path / "memory.db"
    m1 = PersistentVectorMemory(db, scope="ws1", embedder=_embedder)
    m1.add("Write a quarterly report", task_id="t0")
    m2 = PersistentVectorMemory(db, scope="ws2", embedder=_embedder)
    assert len(m2) == 0  # ws2 sees nothing from ws1
    m2.add("Refactor the code", task_id="t1")

    m3 = PersistentVectorMemory(db, scope="ws1", embedder=_embedder)
    assert len(m3) == 1
    hits = m3.search("refactor code", k=1)
    assert hits[0].meta["task_id"] == "t0"  # still ws1's own item
    m1.close(); m2.close(); m3.close()


# -- orchestrator integration ----------------------------------------------


class RecordingProvider(ProviderClient):
    def __init__(self, turns):
        self._turns = list(turns)
        self.prompts: list[str] = []

    def complete(self, *, model, messages, tools=None, **settings):
        self.prompts.append(str((messages or [{}])[-1].get("content", "")))
        assert self._turns, "no scripted turn left"
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


async def test_orchestrator_cross_run_memory(tmp_path):
    """Run 2 (a fresh Orchestrator) retrieves results written by run 1."""
    db = tmp_path / "memory.db"
    scope = "project-a"

    # run 1: completes a report task, writing the outcome to persistent memory.
    p1 = RecordingProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Write a report","deps":[]}]'),
            AssistantTurn(text="report structure: intro, body, conclusion", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}'),
        ]
    )
    o1 = Orchestrator(
        provider=p1, model="m", workspace=str(tmp_path / "ws"),
        memory_scope=scope, memory_db=str(db), governance_config=None,
    )
    r1 = await o1.run("Write a report")
    assert r1.status == "completed"

    # run 2: a brand-new orchestrator on the same scope sees run 1's outcome.
    p2 = RecordingProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Write a report","deps":[]}]'),
            AssistantTurn(text="second report done", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}'),
        ]
    )
    o2 = Orchestrator(
        provider=p2, model="m", workspace=str(tmp_path / "ws"),
        memory_scope=scope, memory_db=str(db), governance_config=None,
    )
    r2 = await o2.run("Write a report")
    assert r2.status == "completed"
    # executor prompt of run 2 carries run 1's stored result as a memory hint.
    exec_prompt = p2.prompts[1]
    assert "report structure" in exec_prompt
