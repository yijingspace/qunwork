"""Durable resume: a prompt pending when the process 'restarts' is answered later and the turn
continues — rebuilt from the persisted thread, with no live await."""

import asyncio

from coworker.providers import (
    AssistantTurn,
    ModelCapabilities,
    ProviderClient,
    ToolCall,
)
from coworker.server.manager import SessionManager


class ScriptedProvider(ProviderClient):
    def __init__(self, turns):
        self._turns = list(turns)

    def complete(self, *, model, messages, tools=None, **settings):
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def _tool(name, args, call_id):
    return AssistantTurn(tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])


def _text(text):
    return AssistantTurn(text=text, finish_reason="stop")


async def _run_until_pending(mgr, sid, engine):
    async def first():
        async for _ in engine.run("go"):
            pass

    task = asyncio.create_task(first())
    pend = []
    for _ in range(100):
        await asyncio.sleep(0.02)
        pend = mgr.inbox.pending(sid)
        if pend:
            break
    assert pend, "prompt never became a pending Inbox item"
    # simulate a restart: cancel the suspended turn + drop the live engine
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    mgr._engines.pop(sid, None)
    mgr.mark_idle(sid)
    return pend[0]


def _final_assistant_texts(mgr, sid):
    rec = mgr.session_store.load(sid)
    return [
        m.get("content")
        for m in rec.messages
        if m.get("role") == "assistant" and m.get("content")
    ]


def test_durable_resume_question(tmp_path):
    mgr = SessionManager(
        workspace=tmp_path,
        provider=ScriptedProvider(
            [
                _tool(
                    "ask_user",
                    {
                        "question": "Which region?",
                        "options": ["us-east-1", "us-west-2"],
                    },
                    "call_q",
                ),
                _text("You chose us-west-2."),
            ]
        ),
    )
    sid = "dur-q"

    async def scenario():
        engine = mgr.get_engine(sid, agent="cowork", workspace=str(tmp_path))
        item = await _run_until_pending(mgr, sid, engine)
        assert item.kind == "question" and item.tool_call_id == "call_q"
        await mgr.resolve_inbox(item.id, "us-west-2")  # restart-style resume

    asyncio.run(scenario())
    assert any("us-west-2" in (t or "") for t in _final_assistant_texts(mgr, sid))
    assert mgr.inbox.pending(sid) == []  # nothing left pending


def test_durable_resume_approval_executes_tool(tmp_path):
    # The model wants a write (needs approval); on durable resume "allow" must RE-EXECUTE the tool.
    target = tmp_path / "scratch_marker.txt"
    mgr = SessionManager(
        workspace=tmp_path,
        provider=ScriptedProvider(
            [
                _tool("write_file", {"path": str(target), "content": "ok"}, "call_w"),
                _text("Done — file written."),
            ]
        ),
    )
    sid = "dur-a"

    async def scenario():
        engine = mgr.get_engine(sid, agent="cowork", workspace=str(tmp_path))
        item = await _run_until_pending(mgr, sid, engine)
        assert item.kind == "approval" and item.tool_call_id == "call_w"
        assert not target.exists()  # not executed before approval
        await mgr.resolve_inbox(
            item.id, "allow"
        )  # restart-style resume → re-execute the tool

    asyncio.run(scenario())
    assert (
        target.exists() and target.read_text() == "ok"
    )  # the approved write actually ran
    assert any("Done" in (t or "") for t in _final_assistant_texts(mgr, sid))


def test_approval_resolution_while_session_running_does_not_duplicate_tool(tmp_path):
    """Regression (owner-hit 2026-08-10): a scheduled run parks on an approval while
    the session is STILL marked running. Resolving that approval must NOT durable-resume
    the session — durable resume would replay the still-suspended tool_calls AND the
    original engine.run() would also wake up, executing the same calls twice. Duplicate
    tool_call_id in the history makes the provider reject the whole turn (HTTP 400
    "Duplicate value for 'tool_call_id'"). The fix marks the session running for the
    whole scheduled run; resolving the approval then only releases the original suspend.
    """
    target = tmp_path / "solo_marker.txt"
    mgr = SessionManager(
        workspace=tmp_path,
        provider=ScriptedProvider(
            [
                _tool("write_file", {"path": str(target), "content": "once"}, "call_w"),
                _text("Done — wrote it once."),
            ]
        ),
    )
    async def scenario(running: bool, mgr):
        sid = "sched-live"
        engine = mgr.get_engine(sid, agent="cowork", workspace=str(tmp_path))
        if running:
            mgr.mark_running(sid)  # what _run_scheduled_task now does for the whole run
        task = asyncio.create_task(_drain(engine))
        pend = None
        for _ in range(200):
            await asyncio.sleep(0.02)
            pend = mgr.inbox.pending(sid)
            if pend:
                break
        assert pend, "approval prompt never became pending"
        await mgr.resolve_inbox(pend[0].id, "allow")
        await task
        return len(
            [
                m
                for m in engine.messages
                if m.get("role") == "tool" and m.get("tool_call_id") == "call_w"
            ]
        )

    # WITHOUT the running claim (pre-fix): resolution durable-resumes the
    # suspended session AND wakes the original engine.run — the same call
    # executes twice → duplicate tool_call_id in history.
    mgr_a = SessionManager(
        workspace=tmp_path, data_dir=tmp_path / "data-a",
        provider=ScriptedProvider([
            _tool("write_file", {"path": str(target), "content": "once"}, "call_w"),
            _text("Done — wrote it once."),
        ]),
    )
    assert asyncio.run(scenario(running=False, mgr=mgr_a)) == 2
    # WITH the running claim (the fix): resolution only releases the original
    # suspend — exactly one tool result, no duplicate.
    mgr_b = SessionManager(
        workspace=tmp_path, data_dir=tmp_path / "data-b",
        provider=ScriptedProvider([
            _tool("write_file", {"path": str(target), "content": "once"}, "call_w"),
            _text("Done — wrote it once."),
        ]),
    )
    assert asyncio.run(scenario(running=True, mgr=mgr_b)) == 1
    assert target.exists() and target.read_text() == "once"


async def _drain(engine):
    async for _ in engine.run("go"):
        pass
