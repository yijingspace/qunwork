"""Orchestrator (multi-agent loop cooperation) tests — Phase 1 MVP."""

import pytest

from coworker.orchestrator import Orchestrator
from coworker.orchestrator.orchestrator import parse_plan, parse_verdict
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient


class ScriptedProvider(ProviderClient):
    """Pops scripted turns in order; each worker engine consumes one turn."""

    def __init__(self, turns):
        self._turns = list(turns)

    def complete(self, *, model, messages, tools=None, **settings):
        assert self._turns, "no scripted turn left"
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def _orch(tmp_path, provider, **kw):
    return Orchestrator(
        provider=provider,
        model="test-model",
        workspace=str(tmp_path / "ws"),
        **kw,
    )


# -- parsing ----------------------------------------------------------------


def test_parse_plan_from_fenced_json():
    plan = parse_plan(
        '```json\n[{"id":"t0","description":"A","deps":[]},{"id":"t1","description":"B","deps":["t0"]}]\n```',
        goal="g",
    )
    assert [t.id for t in plan.tasks] == ["t0", "t1"]
    assert plan.tasks[1].deps == ["t0"]
    assert plan.tasks[0].ready(plan.by_id())
    assert not plan.tasks[1].ready(plan.by_id())


def test_parse_plan_rejects_empty():
    with pytest.raises(ValueError):
        parse_plan("[]", goal="g")


def test_parse_verdict():
    v = parse_verdict('{"accepted": false, "confidence": 0.4, "reason": "missing data", "needs_human": true}')
    assert not v.accepted and v.needs_human and v.reason == "missing data"


# -- orchestration flow -----------------------------------------------------


async def test_completes_two_task_plan(tmp_path):
    provider = ScriptedProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Write outline","deps":[]},'
                                 '{"id":"t1","description":"Write report","deps":["t0"]}]'),
            AssistantTurn(text="Outline: 1. Intro 2. Body", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"outline ok","needs_human":false}'),
            AssistantTurn(text="Report draft complete.", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.95,"reason":"complete","needs_human":false}'),
        ]
    )
    orch = _orch(tmp_path, provider)
    result = await orch.run("Write a short report")
    assert result.status == "completed"
    assert all(t.done for t in result.plan.tasks)
    assert result.runs == 2
    assert "Write outline" in result.task_report()


async def test_requeues_on_rejected_verdict(tmp_path):
    provider = ScriptedProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Fix the bug","deps":[]}]'),
            AssistantTurn(text="attempt 1: patch applied", finish_reason="stop"),
            AssistantTurn(text='{"accepted":false,"confidence":0.3,"reason":"tests still fail","needs_human":false}'),
            AssistantTurn(text="attempt 2: fixed the root cause", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"tests pass now","needs_human":false}'),
        ]
    )
    orch = _orch(tmp_path, provider)
    result = await orch.run("Fix the failing test")
    assert result.status == "completed"
    task = result.plan.tasks[0]
    assert task.done and task.retries == 1
    assert "attempt 2" in task.result


async def test_escalates_to_human(tmp_path):
    provider = ScriptedProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Delete prod row","deps":[]}]'),
            AssistantTurn(text="attempted the deletion", finish_reason="stop"),
            AssistantTurn(text='{"accepted":false,"confidence":0.1,"reason":"needs sign-off","needs_human":true}'),
        ]
    )
    orch = _orch(tmp_path, provider)
    result = await orch.run("Remove the row")
    assert result.status == "needs_human"
    assert result.plan.tasks[0].status == "needs_human"


async def test_retry_exhaustion_escalates(tmp_path):
    provider = ScriptedProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Parse the file","deps":[]}]'),
            AssistantTurn(text="bad attempt 1", finish_reason="stop"),
            AssistantTurn(text='{"accepted":false,"confidence":0.2,"reason":"wrong format","needs_human":false}'),
            AssistantTurn(text="bad attempt 2", finish_reason="stop"),
            AssistantTurn(text='{"accepted":false,"confidence":0.2,"reason":"still wrong","needs_human":false}'),
            AssistantTurn(text="bad attempt 3", finish_reason="stop"),
            AssistantTurn(text='{"accepted":false,"confidence":0.2,"reason":"still wrong","needs_human":false}'),
        ]
    )
    orch = _orch(tmp_path, provider, max_retries=2)
    result = await orch.run("Parse the file")
    assert result.status == "needs_human"
    assert result.plan.tasks[0].retries == 3  # initial + 2 retries


async def test_executor_error_escalates_after_retries(tmp_path):
    class FailingExecutor(ProviderClient):
        def __init__(self):
            self.planned = False

        def complete(self, *, model, messages, tools=None, **settings):
            # First call is the planner → return the plan; every executor attempt
            # then raises so the orchestrator exercises its error retry path.
            if not self.planned:
                self.planned = True
                return AssistantTurn(text='[{"id":"t0","description":"Do thing","deps":[]}]')
            raise RuntimeError("provider boom")

        def capabilities(self, model):
            return ModelCapabilities()

    orch = _orch(tmp_path, FailingExecutor(), max_retries=1)
    result = await orch.run("Do the thing")
    assert result.status == "needs_human"
    assert "executor error" in result.plan.tasks[0].result


# -- governance integration -------------------------------------------------


async def test_governance_pauses_on_red_line(tmp_path):
    from coworker.orchestrator.governance import GovernanceConfig

    provider = ScriptedProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Clean DB — drop table logs","deps":[]}]'),
            AssistantTurn(text="attempting the deletion", finish_reason="stop"),
        ]
    )
    orch = _orch(
        tmp_path,
        provider,
        governance_config=GovernanceConfig(check_every=1, red_lines=["drop table"]),
    )
    result = await orch.run("Clean up the database")
    assert result.status == "paused"
    assert "PAUSE" in result.governance_report
    assert "red-line" in result.governance_report


async def test_governance_revert_redispatch_low_confidence(tmp_path):
    """A REVERT command re-dispatches the latest low-confidence done task."""
    from coworker.orchestrator.governance import GovernanceConfig

    # t0 finishes with the LOWEST confidence; t1's identical (stuck) output pushes
    # viscosity over the threshold before the plan completes → REVERT re-dispatches
    # t0 (lowest confidence), which then completes properly, and t2 runs after.
    provider = ScriptedProvider(
        [
            AssistantTurn(text='[{"id":"t0","description":"Write intro","deps":[]},'
                                 '{"id":"t1","description":"Draft body","deps":["t0"]},'
                                 '{"id":"t2","description":"Final report","deps":["t1"]}]'),
            AssistantTurn(text="stuck output", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.2,"reason":"ok","needs_human":false}'),
            AssistantTurn(text="stuck output", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.3,"reason":"ok","needs_human":false}'),
            AssistantTurn(text="final good intro", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"complete","needs_human":false}'),
            AssistantTurn(text="final report body", finish_reason="stop"),
            AssistantTurn(text='{"accepted":true,"confidence":0.95,"reason":"complete","needs_human":false}'),
        ]
    )
    orch = _orch(
        tmp_path,
        provider,
        governance_config=GovernanceConfig(
            check_every=1, viscosity_high=0.8, viscosity_mid=0.4,
            drift_threshold=1.0, max_warnings=99
        ),
    )
    result = await orch.run("Write a report")
    assert "REVERT" in result.governance_report
    assert result.status == "completed"
    # The re-dispatched t0 finished with the high-confidence result.
    t0 = result.plan.tasks[0]
    assert t0.done and "final good intro" in t0.result


def test_orchestrate_mounted_on_knowledge_engine(tmp_path):
    """The orchestrate tool is mounted into knowledge-family engines (desktop
    integration): a user can delegate a whole goal to the worker swarm in chat."""
    from coworker.agent import build_engine
    from coworker.agents import get_agent

    class P(ProviderClient):
        def complete(self, *, model, messages, tools=None, **settings):
            return AssistantTurn(text="ok", finish_reason="stop")

        def capabilities(self, model):
            return ModelCapabilities()

    ws = tmp_path / "ws"
    ws.mkdir()
    engine = build_engine(agent=get_agent("cowork"), workspace=str(ws), provider=P())
    names = engine.registry.names()
    assert "orchestrate" in names  # knowledge family can fan out to the swarm
    # Code family keeps its explorer-only delegation.
    code_engine = build_engine(agent=get_agent("code"), workspace=str(ws), provider=P())
    assert "orchestrate" not in code_engine.registry.names()


async def test_executor_writes_are_auto_approved(tmp_path):
    """Swarm workers auto-approve writes (no deadlock waiting for a human click):
    an executor that calls write_file completes the task instead of hanging."""
    from coworker.engine import ApprovalOutcome
    from coworker.orchestrator import auto_approver

    approver = auto_approver()
    # exercise the approver directly: a write request resolves to ALWAYS_TOOL
    from coworker.engine import PermissionRequest

    outcome = await approver(
        PermissionRequest(
            tool_name="write_file",
            arguments={"path": "x.txt"},
            metadata=None,
            reason="test",
        )
    )
    assert outcome == ApprovalOutcome.ALWAYS_TOOL


async def test_orchestrator_timeout_pauses(tmp_path):
    """A run exceeding timeout_seconds returns paused instead of hanging forever."""
    from coworker.orchestrator import Orchestrator
    from coworker.orchestrator.governance import GovernanceConfig
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient

    class SlowProvider(ProviderClient):
        def complete(self, *, model, messages, tools=None, **settings):
            import time

            time.sleep(0.5)  # slow enough to trip a 0.1s timeout
            return AssistantTurn(text='[{"id":"t0","description":"Write a report","deps":[]}]')

        def capabilities(self, model):
            return ModelCapabilities()

    orch = Orchestrator(
        provider=SlowProvider(),
        model="m",
        workspace=str(tmp_path / "ws"),
        timeout_seconds=1,
        governance_config=GovernanceConfig(),
    )
    result = await orch.run("Write a report")
    assert result.status == "paused"
    assert "timed out" in result.summary


async def test_task_timeout_degrades_and_continues(tmp_path):
    """A task that exceeds task_timeout_seconds is marked done (partial) so the
    rest of the plan and the final report still proceed."""
    from coworker.orchestrator import Orchestrator
    from coworker.orchestrator.governance import GovernanceConfig
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient

    class SlowExecutor(ProviderClient):
        def __init__(self):
            self.planned = False
            self.exec_calls = 0

        def complete(self, *, model, messages, tools=None, **settings):
            last = str((messages or [{}])[-1].get("content", ""))
            if "Execute it now" in last:  # executor
                import time

                time.sleep(0.4)  # exceed the 0.2s intent; task timeout is 1s
                return AssistantTurn(text="never reached", finish_reason="stop")
            if "Validate the result" in last:  # reviewer
                return AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}')
            return AssistantTurn(text='[{"id":"t0","description":"Write intro","deps":[]}]')

        def capabilities(self, model):
            return ModelCapabilities()

    orch = Orchestrator(
        provider=SlowExecutor(),
        model="m",
        workspace=str(tmp_path / "ws"),
        task_timeout_seconds=1,
        timeout_seconds=30,
        governance_config=GovernanceConfig(),
    )
    # the executor sleeps 0.4s per call; a tight task timeout forces degradation
    orch.task_timeout_seconds = 0.2
    result = await orch.run("Write a report")
    assert result.status == "completed"  # degraded, not paused
    t0 = result.plan.tasks[0]
    assert t0.done and "timed out" in t0.result
