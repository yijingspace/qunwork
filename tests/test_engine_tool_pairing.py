"""Verify the engine never sends orphaned tool_calls to a provider:
every assistant message with tool_calls must be followed by a tool message
responding to each tool_call_id (OpenAI rejects mismatches with 400).

Reproduces the multi-turn worker path (planner/executor style engines)."""

from coworker.orchestrator.workers import build_planner_engine
from coworker.providers import ToolCall
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient


class RecordingProvider(ProviderClient):
    def __init__(self, turns):
        self._turns = list(turns)
        self.seen: list[list[dict]] = []

    def complete(self, *, model, messages, tools=None, **settings):
        self.seen.append(list(messages))
        assert self._turns, "no scripted turn left"
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def _pairing_ok(messages: list[dict]) -> bool:
    """Every assistant tool_calls message has matching tool responses."""
    for i, m in enumerate(messages):
        tcs = m.get("tool_calls") or []
        if not tcs:
            continue
        ids = {tc.get("id") for tc in tcs}
        # collect tool responses after this message until next assistant
        responded = set()
        for nxt in messages[i + 1 :]:
            if nxt.get("role") == "assistant":
                break
            if nxt.get("role") == "tool" and nxt.get("tool_call_id") in ids:
                responded.add(nxt["tool_call_id"])
        if responded != ids:
            return False
    return True


def test_multi_turn_tool_calls_are_paired(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hello world", encoding="utf-8")

    provider = RecordingProvider(
        [
            # turn 1: assistant asks to read two files
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"}),
                    ToolCall(id="c2", name="list_files", arguments={}),
                ]
            ),
            # turn 2: assistant done
            AssistantTurn(text="done", finish_reason="stop"),
        ]
    )
    engine = build_planner_engine(workspace=str(ws), provider=provider, model="m")
    import asyncio

    async def _run():
        async for _ in engine.run("Plan a task"):
            pass

    asyncio.run(_run())

    assert len(provider.seen) == 2
    # the second provider call (after tool execution) must have complete pairing
    assert _pairing_ok(provider.seen[1]), "orphaned tool_calls detected in provider feed"
    # and the persisted messages too
    assert _pairing_ok(engine.messages), "orphaned tool_calls persisted"


def test_interrupted_tool_call_is_answered(tmp_path):
    """A tool call cancelled mid-turn still gets an error tool message."""
    from coworker.engine import TurnEngine
    from coworker.orchestrator.workers import build_planner_engine

    ws = tmp_path / "ws"
    ws.mkdir()

    provider = RecordingProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="c9", name="read_file", arguments={"path": "missing.txt"}),
                ]
            ),
        ]
    )
    engine = build_planner_engine(workspace=str(ws), provider=provider, model="m")
    assert isinstance(engine, TurnEngine)

    import asyncio

    async def _run():
        engine._cancel.set()  # simulate user stop before execution
        async for _ in engine.run("Plan"):
            pass

    asyncio.run(_run())
    assert _pairing_ok(engine.messages), "interrupted tool_call left unpaired"


def test_main_agent_orchestrate_tool_full_chain(tmp_path):
    """Full chain: the main agent calls the orchestrate tool; the tool runs worker
    engines on the SAME provider. Every provider feed must have complete tool
    pairing (a mismatch is what OpenAI rejects with the 400 the user hit)."""
    import asyncio

    from coworker.agent import build_engine
    from coworker.agents import get_agent
    from coworker.orchestrator import orchestration_tools

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hello", encoding="utf-8")

    plan = '[{"id":"t0","description":"Write intro","deps":[]}]'
    executor_out = "intro written"
    verdict = '{"accepted":true,"confidence":0.9,"reason":"ok","needs_human":false}'

    class ChainProvider(ProviderClient):
        def __init__(self):
            self.turns = [
                # 1: main agent asks to call orchestrate
                AssistantTurn(
                    tool_calls=[
                        ToolCall(id="c0", name="orchestrate", arguments={"intent": "Write a report"})
                    ],
                    finish_reason="tool_calls",
                ),
                # 2: planner inside the tool
                AssistantTurn(text=plan, finish_reason="stop"),
                # 3: executor
                AssistantTurn(text=executor_out, finish_reason="stop"),
                # 4: reviewer
                AssistantTurn(text=verdict, finish_reason="stop"),
                # 5: main agent concludes
                AssistantTurn(text="All done.", finish_reason="stop"),
            ]
            self.seen: list[list[dict]] = []

        def complete(self, *, model, messages, tools=None, **settings):
            self.seen.append(list(messages))
            assert self.turns, "no scripted turn left"
            return self.turns.pop(0)

        def capabilities(self, model):
            return ModelCapabilities()

    provider = ChainProvider()
    engine = build_engine(
        agent=get_agent("cowork"),
        workspace=str(ws),
        provider=provider,
        extra_tools=orchestration_tools(workspace=str(ws), provider=provider, model="m"),
        approver=None,
    )

    async def _run():
        async for _ in engine.run("Write a report"):
            pass

    asyncio.run(_run())

    assert len(provider.seen) >= 3  # main turn1, planner, executor, reviewer, main turn2
    for i, msgs in enumerate(provider.seen):
        assert _pairing_ok(msgs), f"orphaned tool_calls in provider feed #{i}"
    assert _pairing_ok(engine.messages), "main engine persisted orphaned tool_calls"


def test_outbound_heals_orphaned_tool_calls():
    """The defensive pass guarantees a provider never sees an orphaned tool_call."""
    from coworker.engine import _heal_tool_pairing

    broken = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "x"}}, {"id": "c2", "function": {"name": "y"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},  # c2 missing
        {"role": "assistant", "content": "done"},
    ]
    healed = _heal_tool_pairing(broken)
    assert _pairing_ok(healed)
    # the placeholder response for c2 is present
    tids = [m["tool_call_id"] for m in healed if m.get("role") == "tool"]
    assert "c2" in tids
    # no mutation of the input
    assert len(broken) == 4 and "c2" not in [m.get("tool_call_id") for m in broken]
