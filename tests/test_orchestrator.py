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
        max_parallel=1,  # serial: this test scripts deterministic turn order
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
    """A task that exceeds task_timeout_seconds is marked done with the draft the
    executor already produced, so dependents and the final report still proceed."""
    from coworker.orchestrator import Orchestrator
    from coworker.orchestrator.governance import GovernanceConfig
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient, ToolCall

    class SlowExecutor(ProviderClient):
        def __init__(self):
            self.first_exec = True
            self.drafted = False

        def complete(self, *, model, messages, tools=None, **settings):
            joined = str(messages)
            if "Validate the result" in joined:  # reviewer
                return AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}')
            if self.first_exec:  # planner
                self.first_exec = False
                return AssistantTurn(text='[{"id":"t0","description":"Write intro","deps":[]}]')
            if not self.drafted:  # executor, first call: draft + tool call
                self.drafted = True
                return AssistantTurn(
                    text="初稿:中国AI市场规模约1.2万亿",
                    tool_calls=[ToolCall(id="c1", name="list_files", arguments={})],
                )
            import time

            time.sleep(0.4)  # executor second call exceeds the 0.2s task timeout
            return AssistantTurn(text="核实中", finish_reason="stop")

        def capabilities(self, model):
            return ModelCapabilities()

    orch = Orchestrator(
        provider=SlowExecutor(),
        model="m",
        workspace=str(tmp_path / "ws"),
        task_timeout_seconds=0.2,
        timeout_seconds=30,
        governance_config=GovernanceConfig(),
    )
    result = await orch.run("Write a report")
    assert result.status == "completed"  # degraded, not paused
    t0 = result.plan.tasks[0]
    assert t0.done
    # the executor's real draft survived the timeout (not a placeholder)
    assert "1.2万亿" in t0.result


def test_create_skill_persists_and_reloads(tmp_path):
    """create_skill writes a SKILL.md, refreshes the catalog, and the skill is
    immediately loadable — the worker's self-authored toolbelt survives runs."""
    from coworker.skills import SkillLoader, skill_tools

    ws = tmp_path / "ws"
    ws.mkdir()
    loader = SkillLoader([ws / ".coworker" / "skills", ws])
    tools = {getattr(t, "__name__", ""): t for t in skill_tools(loader)}
    assert "create_skill" in tools and "load_skill" in tools

    out = tools["create_skill"](
        name="ps-quoting",
        description="PowerShell quoting helper for Windows",
        body="Use single quotes outside, escape `$` with backtick...",
    )
    assert out["ok"] and "skills" in out["path"]

    # skill is now in the catalog and loadable
    assert "ps-quoting" in loader.names()
    loaded = tools["load_skill"]("ps-quoting")
    assert loaded["name"] == "ps-quoting" and "single quotes" in loaded["instructions"]
    assert (ws / ".coworker" / "skills" / "ps-quoting" / "SKILL.md").is_file()


def test_parse_plan_recovers_truncated_json():
    """A truncated task array (max_tokens cut) is repaired, not fatal."""
    from coworker.orchestrator.orchestrator import parse_plan

    plan = parse_plan(
        '[{"id":"t0","description":"Write intro","deps":[]},{"id":"t1","description":"Write body","deps":["t0"]}',
        goal="g",
    )
    assert [t.id for t in plan.tasks] == ["t0", "t1"]


async def test_planner_retries_then_succeeds(tmp_path):
    """A planner that returns garbage on the first call is retried."""
    from coworker.orchestrator import Orchestrator
    from coworker.orchestrator.governance import GovernanceConfig
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient

    class FlakyPlanner(ProviderClient):
        def __init__(self):
            self.calls = 0

        def complete(self, *, model, messages, tools=None, **settings):
            joined = str(messages)
            if "Validate the result" in joined:  # reviewer
                return AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}')
            if "Execute it now" in joined:  # executor
                return AssistantTurn(text="chapter done", finish_reason="stop")
            self.calls += 1  # planner
            if self.calls == 1:
                return AssistantTurn(text="not json at all", finish_reason="stop")
            return AssistantTurn(text='[{"id":"t0","description":"Write intro","deps":[]}]')

        def capabilities(self, model):
            return ModelCapabilities()

    provider = FlakyPlanner()
    orch = Orchestrator(
        provider=provider,
        model="m",
        workspace=str(tmp_path / "ws"),
        governance_config=GovernanceConfig(),
    )
    result = await orch.run("Write a report")
    assert result.status == "completed"
    assert provider.calls >= 2  # first planner call failed, second succeeded


async def test_global_timeout_keeps_partial_drafts(tmp_path):
    """When the whole run times out, already-completed task results are kept."""
    from coworker.orchestrator import Orchestrator
    from coworker.orchestrator.governance import GovernanceConfig
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient, ToolCall

    class Slow(ProviderClient):
        def __init__(self):
            self.done_t1 = False

        def complete(self, *, model, messages, tools=None, **settings):
            joined = str(messages)
            if "Validate the result" in joined:
                return AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}')
            if "Execute it now" in joined:
                is_t1 = "Task [t1]" in joined
                if is_t1 and self.done_t1:
                    import time

                    time.sleep(2.0)  # t1 hangs past the 1s global timeout
                    return AssistantTurn(text="never", finish_reason="stop")
                if is_t1:
                    self.done_t1 = True
                    return AssistantTurn(
                        text="t1 草稿",
                        tool_calls=[ToolCall(id="c1", name="list_files", arguments={})],
                    )
                # t0 completes quickly with a real result
                if "Task [t0]" in joined:
                    return AssistantTurn(text="t0 章节草稿:固态电池2027量产", finish_reason="stop")
                return AssistantTurn(text="草稿", finish_reason="stop")
            return AssistantTurn(text='[{"id":"t0","description":"Write A","deps":[]},'
                                       '{"id":"t1","description":"Write B","deps":[]}]')

        def capabilities(self, model):
            return ModelCapabilities()

    orch = Orchestrator(
        provider=Slow(),
        model="m",
        workspace=str(tmp_path / "ws"),
        timeout_seconds=1,  # short global timeout
        task_timeout_seconds=10,  # only the global timeout fires
        governance_config=GovernanceConfig(),
    )
    result = await orch.run("Write a report")
    assert result.status == "paused"
    # the completed task's result survived the global timeout
    assert any("固态电池2027量产" in t.result for t in result.plan.tasks)


def test_parse_plan_falls_back_to_description_fields():
    """A non-JSON planner reply with description fields still yields a plan."""
    from coworker.orchestrator.orchestrator import parse_plan

    plan = parse_plan(
        'Here is the plan: {"description": "Write intro"}, {"description": "Write body"}',
        goal="g",
    )
    assert [t.description for t in plan.tasks] == ["Write intro", "Write body"]


async def test_report_is_persisted_to_workspace(tmp_path):
    """The assembled report is always written to _swarm_reports in the workspace."""
    from coworker.orchestrator import Orchestrator
    from coworker.orchestrator.governance import GovernanceConfig
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient

    class P(ProviderClient):
        def complete(self, *, model, messages, tools=None, **settings):
            joined = str(messages)
            if "Validate the result" in joined:
                return AssistantTurn(text='{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}')
            if "Execute it now" in joined:
                return AssistantTurn(text="完整报告:固态电池市场分析……", finish_reason="stop")
            return AssistantTurn(text='[{"id":"t0","description":"Write report","deps":[]}]')

        def capabilities(self, model):
            return ModelCapabilities()

    orch = Orchestrator(
        provider=P(),
        model="m",
        workspace=str(tmp_path / "ws"),
        governance_config=GovernanceConfig(),
    )
    result = await orch.run("撰写固态电池市场分析报告")
    assert result.status == "completed"
    assert result.report_path
    saved = __import__("pathlib").Path(result.report_path)
    assert saved.is_file()
    assert "固态电池" in saved.read_text(encoding="utf-8")
    assert "_swarm_reports" in result.report_path
