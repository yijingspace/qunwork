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


# -- requeue→Inbox bridge ------------------------------------------------------
def _bridged(tmp_path):
    from coworker.inbox import InboxStore

    inbox = InboxStore(tmp_path / "inbox.json")
    ctrl = RunController()
    ctrl.attach_inbox_bridge(
        inbox, session_id="sess1", run_id="orch_x", hold_seconds=30.0
    )
    return ctrl, inbox


def test_requeue_inbox_allow_reruns(tmp_path):
    """A bridged rejection parks on a durable Inbox approval; 'allow' → requeue."""

    async def scenario():
        ctrl, inbox = _bridged(tmp_path)
        t = asyncio.create_task(
            ctrl.await_requeue("t1", {"attempt": 2, "reason": "weak"}, timeout=0.1)
        )
        # the deck-only 30s timeout must NOT fire — instead an Inbox card appears.
        for _ in range(50):
            pend = inbox.pending("sess1")
            if pend:
                break
            await asyncio.sleep(0.02)
        pend = inbox.pending("sess1")
        assert len(pend) == 1
        assert pend[0].data["swarm_requeue"] is True
        assert pend[0].data["task_id"] == "t1"
        inbox.resolve(pend[0].id, "allow")
        assert await asyncio.wait_for(t, timeout=2) is True
        assert inbox.pending("sess1") == []  # card consumed

    asyncio.run(scenario())


def test_requeue_inbox_deny_accepts_current_result(tmp_path):
    """'deny' on the Inbox card → decline (accept the degraded result as-is)."""

    async def scenario():
        ctrl, inbox = _bridged(tmp_path)
        t = asyncio.create_task(ctrl.await_requeue("t1", {"attempt": 1}, timeout=0.1))
        for _ in range(50):
            pend = inbox.pending("sess1")
            if pend:
                break
            await asyncio.sleep(0.02)
        inbox.resolve(pend[0].id, "deny")
        assert await asyncio.wait_for(t, timeout=2) is False

    asyncio.run(scenario())


def test_requeue_deck_wins_and_closes_inbox_card(tmp_path):
    """If the command deck answers first, the future resolves AND the now-redundant
    Inbox card is auto-closed (no dangling approval in the queue)."""

    async def scenario():
        ctrl, inbox = _bridged(tmp_path)
        t = asyncio.create_task(ctrl.await_requeue("t1", {"attempt": 1}, timeout=0.1))
        for _ in range(50):
            if inbox.pending("sess1"):
                break
            await asyncio.sleep(0.02)
        assert inbox.pending("sess1"), "inbox card should be open"
        # deck approves via the threadsafe path (not the inbox)
        assert ctrl.approve_requeue("t1") is True
        assert await asyncio.wait_for(t, timeout=2) is True
        await asyncio.sleep(0.05)  # let finally close the card
        assert inbox.pending("sess1") == []  # closed exactly once, idempotently

    asyncio.run(scenario())


def test_requeue_no_bridge_keeps_deck_timeout_semantics(tmp_path):
    """Without a bridge (sync/headless), the original short-timeout decline holds —
    proves the change is opt-in and didn't alter existing behavior."""

    async def scenario():
        from coworker.inbox import InboxStore

        inbox = InboxStore(tmp_path / "i.json")
        ctrl = RunController()  # NOT bridged
        t = asyncio.create_task(ctrl.await_requeue("t1", {}, timeout=0.1))
        assert await asyncio.wait_for(t, timeout=2) is False
        assert inbox.pending() == []  # nothing surfaced to the Inbox

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
            AssistantTurn(text="first draft " + "x"*150, finish_reason="stop"),
            AssistantTurn(text='{"accepted":false,"confidence":0.3,"reason":"weak","needs_human":false}'),
            AssistantTurn(text="second draft " + "x"*150, finish_reason="stop"),
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


def test_orchestrator_requeue_reruns_via_inbox(tmp_path):
    """requeue→Inbox: a reviewer rejection surfaces a durable Inbox approval; the
    operator answering 'allow' THERE (not on the deck) re-runs the task to done."""
    from coworker.inbox import InboxStore

    # Reuse the recording provider pattern from the deck test.
    from coworker.orchestrator import Orchestrator
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient

    class RecordingProvider(ProviderClient):
        def __init__(self, turns):
            self._turns = list(turns)

        def complete(self, *, model, messages, tools=None, **settings):
            return self._turns.pop(0)

        def capabilities(self, model):
            return ModelCapabilities()

    provider = RecordingProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Draft","deps":[]}]'),
            AssistantTurn(text="first draft " + "x" * 150, finish_reason="stop"),
            AssistantTurn(text='{"accepted":false,"confidence":0.3,"reason":"weak","needs_human":false}'),
            AssistantTurn(text="second draft " + "x" * 150, finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}'),
        ]
    )
    inbox = InboxStore(tmp_path / "inbox.json")
    ctrl = RunController()
    ctrl.attach_inbox_bridge(
        inbox, session_id="s1", run_id="orch_t", hold_seconds=15.0
    )

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
            requeue_approval_timeout=0.1,  # deck timeout tiny → only Inbox can save it
            event_sink=lambda kind, payload: None,
        )
        run_task = asyncio.create_task(orch.run("do it"))
        # Wait for the durable Inbox card (NOT the deck) to appear.
        pend = []
        for _ in range(200):
            pend = inbox.pending("s1")
            if pend:
                break
            await asyncio.sleep(0.02)
        assert pend, "a requeue Inbox approval should surface for the rejected task"
        assert pend[0].data.get("swarm_requeue") is True
        # Answer from the Inbox; the tiny deck timeout must NOT have degraded it yet.
        inbox.resolve(pend[0].id, "allow")
        result = await asyncio.wait_for(run_task, timeout=15)
        return result

    result = asyncio.run(scenario())
    assert result.status == "completed"
    t0 = next(t for t in result.plan.tasks if t.id == "t0")
    assert t0.status == "done" and t0.retries == 1  # re-ran, then accepted
    assert inbox.pending("s1") == []  # card consumed
