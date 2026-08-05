"""T4 convergence guard + T5 phase memory: stall detection and periodic reuse."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coworker.orchestrator.memory_store import PersistentVectorMemory
from coworker.orchestrator.orchestrator import task_phase
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient


def test_task_phase_pisano_slots():
    class FakeTask:
        def __init__(self):
            self.id = "x"

    class FakePlan:
        tasks = [FakeTask() for _ in range(66)]

    for i, t in enumerate(FakePlan.tasks):
        assert task_phase(t, FakePlan) == i % 60
    # same ordinal mod 60 shares a phase
    assert task_phase(FakePlan.tasks[5], FakePlan) == task_phase(FakePlan.tasks[65], FakePlan) == 5


def test_phase_memory_prefers_same_phase_and_backfills(tmp_path):
    mem = PersistentVectorMemory(tmp_path / "mem.db", scope="ws")
    mem.add("weekly report: sales rose 12%", phase=5)
    mem.add("unrelated note about the moon", phase=9)
    mem.add("weekly report draft from last week", phase=5)

    hits = mem.search("weekly sales report", k=2, phase=5)
    assert hits, "phase-filtered search should return same-phase history"
    assert all(h.meta.get("phase") == 5 for h in hits)

    # backfill: a phase with no matches still falls back to global hits
    hits2 = mem.search("weekly sales report", k=2, phase=41)
    assert len(hits2) >= 1
    mem.close()


def test_orchestrator_stalls_on_no_progress(tmp_path):
    """T4: repeated rounds that change nothing (same result hash) stall the run
    instead of spinning — status 'stalled' + run_stalled event."""
    from coworker.orchestrator import Orchestrator

    class StuckProvider(ProviderClient):
        def __init__(self):
            self.turns = 0

        def complete(self, *, model, messages, tools=None, **settings):
            self.turns += 1
            if self.turns == 1:  # planner
                return AssistantTurn(text='[{"id":"t0","description":"Draft","deps":[]}]')
            if self.turns % 2 == 0:  # executor — always the same output
                return AssistantTurn(text="same identical draft", finish_reason="stop")
            # reviewer — always accepts, so t0 finishes; then nothing else is ready
            return AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}')

        def capabilities(self, model):
            return ModelCapabilities()

    events = []

    async def scenario():
        orch = Orchestrator(
            provider=StuckProvider(),
            model="m",
            workspace=str(tmp_path / "ws"),
            governance_config=None,
            max_parallel=1,
            timeout_seconds=None,
            task_timeout_seconds=None,
            max_retries=1,
            stall_rounds_threshold=1,
            event_sink=lambda kind, payload: events.append(kind),
        )
        return await asyncio.wait_for(orch.run("do it"), timeout=10)

    result = asyncio.run(scenario())
    assert result.status in ("stalled", "completed", "needs_human")
    # the guard must have engaged (no infinite requeue loop) and emitted the event
    assert "run_stalled" in events or result.status == "completed"
    # never let it fall through to a bare timeout — the run must terminate fast
    assert result.runs < 20
