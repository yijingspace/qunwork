"""Phase 3 tests: vector memory, dependency blackboard, parallel workers."""

import pytest

from coworker.orchestrator import Orchestrator
from coworker.orchestrator.vectormemory import VectorMemory
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient


# -- VectorMemory -----------------------------------------------------------


def _embedder(s: str) -> list[float]:
    # bag-of-words-ish: mark presence of a few keywords
    return [
        1.0 if "report" in s else 0.0,
        1.0 if "code" in s else 0.0,
        1.0 if "test" in s else 0.0,
    ]


def test_memory_search_with_embedder():
    mem = VectorMemory(embedder=_embedder)
    mem.add("Write a quarterly report", task_id="t0")
    mem.add("Refactor the code", task_id="t1")
    hits = mem.search("Prepare a report", k=1)
    assert hits and hits[0].meta["task_id"] == "t0"
    assert hits[0].score > 0.5


def test_memory_search_fallback_difflib():
    mem = VectorMemory()  # no embedder
    mem.add("Write a quarterly report", task_id="t0")
    mem.add("Refactor the code", task_id="t1")
    hits = mem.search("quarterly report", k=1)
    assert hits and hits[0].meta["task_id"] == "t0"


def test_memory_len_and_empty():
    mem = VectorMemory()
    assert len(mem) == 0
    assert mem.search("anything") == []


# -- orchestrator integration ----------------------------------------------


class RecordingProvider(ProviderClient):
    """Records every prompt so tests can assert injected context."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.prompts: list[str] = []

    def complete(self, *, model, messages, tools=None, **settings):
        self.prompts.append(str((messages or [{}])[-1].get("content", "")))
        assert self._turns, "no scripted turn left"
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


async def test_dependency_results_injected_into_executor(tmp_path):
    """A task depending on t0 sees t0's result in its executor prompt."""
    provider = RecordingProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Write intro","deps":[]},'
                                 '{"id":"t1","description":"Write body","deps":["t0"]}]'),
            AssistantTurn(text="intro: hello world", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}'),
            AssistantTurn(text="body done", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}'),
        ]
    )
    orch = Orchestrator(
        provider=provider,
        model="m",
        workspace=str(tmp_path / "ws"),
        governance_config=None,
    )
    result = await orch.run("Write a report")
    assert result.status == "completed"
    # prompts: [planner, exec t0, review t0, exec t1, review t1]
    exec_t1 = provider.prompts[3]
    assert "[t0] intro: hello world" in exec_t1  # dependency result injected


async def test_parallel_execution_runs_independent_tasks_concurrently(tmp_path):
    """max_parallel=2 runs both independent tasks; dependency still serializes.
    Uses a content-aware provider because concurrent worker engines share the
    provider and interleave `complete` calls."""
    import threading

    class ContentAwareProvider(ProviderClient):
        def __init__(self):
            self.calls: list[tuple[str, str]] = []  # (thread, task-hint)

        def complete(self, *, model, messages, tools=None, **settings):
            last = str((messages or [{}])[-1].get("content", ""))
            hint = "validate" if "Validate the result" in last else (
                "A" if "Task [t0]" in last else
                "B" if "Task [t1]" in last else
                "C" if "Task [t2]" in last else
                "plan"
            )
            self.calls.append((threading.current_thread().name, hint))
            if hint == "validate":
                return AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}')
            if hint == "A":
                return AssistantTurn(text="A done", finish_reason="stop")
            if hint == "B":
                return AssistantTurn(text="B done", finish_reason="stop")
            if hint == "C":
                return AssistantTurn(text="C done", finish_reason="stop")
            return AssistantTurn(text='[{"id":"t0","description":"Task A","deps":[]},'
                                       '{"id":"t1","description":"Task B","deps":[]},'
                                       '{"id":"t2","description":"Task C","deps":["t1"]}]')

        def capabilities(self, model):
            return ModelCapabilities()

    provider = ContentAwareProvider()
    orch = Orchestrator(
        provider=provider,
        model="m",
        workspace=str(tmp_path / "ws"),
        max_parallel=2,
        governance_config=None,
    )
    result = await orch.run("Do three things")
    assert result.status == "completed"
    assert all(t.done for t in result.plan.tasks)
    # A and B (independent) ran in the same batch; C ran after B finished.
    hints = [h for _, h in provider.calls]
    assert hints[0] == "plan"
    batch1 = hints[1:5]  # two executors + two reviewers
    assert "A" in batch1 and "B" in batch1
    assert hints.index("C") > hints.index("B")


async def test_code_executor_agent_runs_engineering_task(tmp_path):
    """executor_agent='code' builds the code-persona engine as executor and
    completes an engineering-style task end to end."""
    from coworker.orchestrator.orchestrator import Orchestrator

    class CodeProbe(ProviderClient):
        def __init__(self):
            self.planned = False

        def complete(self, *, model, messages, tools=None, **settings):
            last = str((messages or [{}])[-1].get("content", ""))
            if "Validate the result" in last:
                return AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}')
            if not self.planned:
                self.planned = True
                return AssistantTurn(
                    text='[{"id":"t0","description":"Implement the module","deps":[]}]'
                )
            return AssistantTurn(text="def util(): return 42", finish_reason="stop")

        def capabilities(self, model):
            return ModelCapabilities()

    provider = CodeProbe()
    ws = tmp_path / "ws"
    ws.mkdir()
    orch = Orchestrator(
        provider=provider,
        model="m",
        workspace=str(ws),
        executor_agent="code",
        governance_config=None,
        max_parallel=1,
    )
    result = await orch.run("Write a Python util module")
    assert result.status == "completed"
    assert result.plan.tasks[0].done
    assert "42" in result.plan.tasks[0].result
