"""G2: RunController — pause/resume, operator messages, requeue approval."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coworker.orchestrator.control import RunController


def test_pause_resume():
    ctrl = RunController()
    assert ctrl.paused is False
    ctrl.set_paused(True)
    assert ctrl.paused is True

    async def wait():
        await asyncio.wait_for(ctrl.wait_if_paused(), timeout=0.2)

    # while paused, wait_if_paused blocks
    try:
        asyncio.run(wait())
        assert False, "expected timeout while paused"
    except asyncio.TimeoutError:
        pass
    ctrl.set_paused(False)
    asyncio.run(wait())  # resumes fine


def test_operator_messages():
    ctrl = RunController()
    assert ctrl.drain_messages() == []
    ctrl.inject_message("focus on the summary section")
    ctrl.inject_message("drop the appendix")
    msgs = ctrl.drain_messages()
    assert msgs == ["focus on the summary section", "drop the appendix"]
    assert ctrl.drain_messages() == []  # drained


def test_requeue_approve_and_reject():
    async def scenario(decision: str):
        ctrl = RunController()
        t1 = asyncio.create_task(
            ctrl.await_requeue("t1", {"attempt": 2, "reason": "low confidence"}, timeout=5)
        )
        await asyncio.sleep(0.02)
        pending = ctrl.pending_requeues()
        assert len(pending) == 1 and pending[0]["task_id"] == "t1"
        if decision == "approve":
            assert ctrl.approve_requeue("t1") is True
        else:
            assert ctrl.reject_requeue("t1") is True
        assert await asyncio.wait_for(t1, timeout=1) == (decision == "approve")
        assert ctrl.pending_requeues() == []

    asyncio.run(scenario("approve"))
    asyncio.run(scenario("reject"))


def test_requeue_timeout_declines():
    async def scenario():
        ctrl = RunController()
        t1 = asyncio.create_task(
            ctrl.await_requeue("t1", {"attempt": 1, "reason": "x"}, timeout=0.1)
        )
        assert await asyncio.wait_for(t1, timeout=2) is False  # timeout → decline
        assert ctrl.pending_requeues() == []

    asyncio.run(scenario())


def test_orchestrator_requeue_waits_for_deck_approval(tmp_path):
    """G2: with a RunController attached, a reviewer rejection blocks the task
    (task_requeue_waiting event) until the deck approves — then it re-runs."""
    import asyncio
    from coworker.orchestrator import Orchestrator
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient

    class RecordingProvider(ProviderClient):
        def __init__(self, turns):
            self._turns = list(turns)
            self.prompts = []

        def complete(self, *, model, messages, tools=None, **settings):
            self.prompts.append(str((messages or [{}])[-1].get("content", "")))
            return self._turns.pop(0)

        def capabilities(self, model):
            return ModelCapabilities()

    # planner → exec t0 (bad) → review (REJECT) → exec t0 again → review (accept)
    provider = RecordingProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Draft","deps":[]}]'),
            AssistantTurn(text="first draft", finish_reason="stop"),
            AssistantTurn(text='{"accepted":false,"confidence":0.3,"reason":"weak","needs_human":false}'),
            AssistantTurn(text="second draft", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}'),
        ]
    )
    ctrl = RunController()
    events = []

    async def scenario():
        orch = Orchestrator(
            provider=provider,
            model="m",
            workspace=str(tmp_path / "ws"),
            governance_config=None,
            max_parallel=1,
            timeout_seconds=None,
            task_timeout_seconds=None,
            controller=ctrl,
            requeue_approval_timeout=2.0,
            event_sink=lambda kind, payload: events.append((kind, payload)),
        )
        t1 = asyncio.create_task(orch.run("do it"))
        # wait until the reviewer rejection surfaces as a pending requeue
        for _ in range(100):
            if ctrl.pending_requeues():
                break
            await asyncio.sleep(0.02)
        assert ctrl.pending_requeues(), "requeue should be waiting for the deck"
        # approve → the run continues and completes
        ctrl.approve_requeue("t0")
        result = await asyncio.wait_for(t1, timeout=10)
        return result

    result = asyncio.run(scenario())
    assert result.status == "completed"
    kinds = [k for k, _ in events]
    assert "task_requeue_waiting" in kinds
    assert "task_requeue_approved" in kinds
    t0 = next(t for t in result.plan.tasks if t.id == "t0")
    assert t0.status == "done" and t0.retries == 1
