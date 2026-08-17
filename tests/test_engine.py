"""P2 gate tests — turn engine + event bus (scripted provider, no network)."""

from __future__ import annotations

import asyncio
import threading
import time

import aisuite as ai
from coworker.engine import ApprovalOutcome, PermissionRequest, TurnEngine
from coworker.events import EventType
from coworker.permissions import PermissionEngine
from coworker.providers import (
    AssistantTurn,
    ModelCapabilities,
    ProviderClient,
    StreamChunk,
    ToolCall,
)
from coworker.tools import ToolRegistry


def _text_turn(text):
    return AssistantTurn(text=text, finish_reason="stop")


def _tool_turn(name, args, call_id="call_1"):
    return AssistantTurn(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
        finish_reason="tool_calls",
    )


class ScriptedProvider(ProviderClient):
    """Returns queued AssistantTurns; streams via the base default (one final chunk)."""

    def __init__(self, turns, *, loop=False):
        self._turns = list(turns)
        self._loop = loop
        self.calls = 0

    def complete(self, *, model, messages, tools=None, **settings):
        self.calls += 1
        return self._turns[0] if self._loop else self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def _engine(tmp_path, turns, *, approver=None, loop=False, max_iterations=12):
    provider = ScriptedProvider(turns, loop=loop)
    registry = ToolRegistry()
    registry.register_all(ai.toolkits.files(root=str(tmp_path), allow_write=True))
    permissions = PermissionEngine(workspace_root=tmp_path)
    engine = TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model="gpt-5.5",
        approver=approver,
        max_iterations=max_iterations,
    )
    return engine, provider


def _collect(engine, user_input):
    async def _run():
        return [ev async for ev in engine.run(user_input)]

    return asyncio.run(_run())


def _types(events):
    return [ev.type for ev in events]


# -- tests ----------------------------------------------------------------------


def test_no_tool_turn(tmp_path):
    engine, _ = _engine(tmp_path, [_text_turn("all done")])
    events = _collect(engine, "hi")
    assert _types(events) == [
        EventType.TURN_START,
        EventType.ASSISTANT_MESSAGE,
        EventType.TURN_END,
    ]
    assert events[1].data["text"] == "all done"
    assert events[-1].data["status"] == "completed"


def test_tool_turn_order_and_execution(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    engine, _ = _engine(
        tmp_path,
        [_tool_turn("read_file", {"path": "a.txt"}), _text_turn("it says hello")],
    )
    events = _collect(engine, "read a.txt")
    assert EventType.PERMISSION_REQUIRED not in _types(events)
    assert _types(events) == [
        EventType.TURN_START,
        EventType.ASSISTANT_MESSAGE,
        EventType.TOOL_PROPOSED,
        EventType.TOOL_STARTED,
        EventType.TOOL_FINISHED,
        EventType.ITERATION_END,
        EventType.ASSISTANT_MESSAGE,
        EventType.TURN_END,
    ]
    finished = next(e for e in events if e.type == EventType.TOOL_FINISHED)
    assert finished.data["status"] == "ok"
    assert any(
        m.get("role") == "tool" and "hello" in m["content"] for m in engine.messages
    )


def test_write_requires_approval_then_approved(tmp_path):
    async def approve_once(_req: PermissionRequest):
        return ApprovalOutcome.ONCE

    engine, _ = _engine(
        tmp_path,
        [
            _tool_turn("write_file", {"path": "new.py", "content": "print(1)\n"}),
            _text_turn("wrote new.py"),
        ],
        approver=approve_once,
    )
    events = _collect(engine, "create new.py")
    assert EventType.PERMISSION_REQUIRED in _types(events)
    assert (tmp_path / "new.py").read_text() == "print(1)\n"


def test_denied_tool_yields_error_and_continues(tmp_path):
    async def deny(_req: PermissionRequest):
        return ApprovalOutcome.DENY

    engine, _ = _engine(
        tmp_path,
        [
            _tool_turn("write_file", {"path": "new.py", "content": "x"}),
            _text_turn("ok, skipped it"),
        ],
        approver=deny,
    )
    events = _collect(engine, "create new.py")
    assert not (tmp_path / "new.py").exists()
    finished = next(e for e in events if e.type == EventType.TOOL_FINISHED)
    assert finished.data["status"] == "denied"
    assert _types(events)[-1] == EventType.TURN_END
    assert any(
        m.get("role") == "tool" and "not executed" in m["content"]
        for m in engine.messages
    )


def test_max_iterations_rail(tmp_path):
    engine, provider = _engine(
        tmp_path, [_tool_turn("list_files", {})], loop=True, max_iterations=3
    )
    events = _collect(engine, "loop forever")
    end = events[-1]
    assert end.type == EventType.TURN_END
    assert end.data["status"] == "max_iterations_exceeded"
    assert provider.calls == 3


def test_interrupt_between_iterations(tmp_path):
    engine_holder = {}

    async def approve_and_interrupt(_req: PermissionRequest):
        engine_holder["engine"].request_interrupt()
        return ApprovalOutcome.ONCE

    engine, provider = _engine(
        tmp_path,
        [
            _tool_turn("write_file", {"path": "x.py", "content": "x"}),
            _text_turn("should not be reached"),
        ],
        approver=approve_and_interrupt,
    )
    engine_holder["engine"] = engine
    events = _collect(engine, "do a thing")
    assert events[-1].type == EventType.INTERRUPTED
    assert provider.calls == 1


def test_steering_injects_next_turn(tmp_path):
    engine, provider = _engine(tmp_path, [_text_turn("first"), _text_turn("second")])
    engine.queue_steering("actually, also do this")
    events = _collect(engine, "do the first thing")
    assert provider.calls == 2
    assert any(
        m.get("role") == "user" and m["content"] == "actually, also do this"
        for m in engine.messages
    )
    assert events[-1].data["status"] == "completed"


# -- parallel tool execution ------------------------------------------------------


def _multi_tool_turn(calls):
    return AssistantTurn(
        tool_calls=[
            ToolCall(id=f"call_{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(calls)
        ],
        finish_reason="tool_calls",
    )


def _bare_engine(tmp_path, turns):
    provider = ScriptedProvider(turns)
    registry = ToolRegistry()
    permissions = PermissionEngine(workspace_root=tmp_path)
    engine = TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model="gpt-5.5",
    )
    return engine, registry


def test_low_risk_tool_calls_run_concurrently(tmp_path):
    # Both tools block on a 2-party barrier: the turn only completes if the engine
    # really runs them at the same time (sequential execution would trip the timeout
    # and surface as an error result).
    barrier = threading.Barrier(2, timeout=5)
    low = ai.ToolMetadata(category="search", risk_level="low", requires_approval=False)

    def side_a():
        """Wait for side_b."""
        barrier.wait()
        return {"side": "a"}

    def side_b():
        """Wait for side_a."""
        barrier.wait()
        return {"side": "b"}

    engine, registry = _bare_engine(
        tmp_path,
        [_multi_tool_turn([("side_a", {}), ("side_b", {})]), _text_turn("done")],
    )
    registry.register(side_a, metadata=low)
    registry.register(side_b, metadata=low)

    events = _collect(engine, "go")
    finished = [e for e in events if e.type == EventType.TOOL_FINISHED]
    assert len(finished) == 2
    assert all(e.data["status"] == "ok" for e in finished)
    # a tool result message exists for every call id
    tool_ids = {
        m.get("tool_call_id") for m in engine.messages if m.get("role") == "tool"
    }
    assert tool_ids == {"call_0", "call_1"}


def test_non_low_risk_tool_calls_stay_sequential(tmp_path):
    order = []
    medium = ai.ToolMetadata(
        category="filesystem", risk_level="medium", requires_approval=False
    )

    def first():
        """Record start/end with a delay."""
        order.append("first-start")
        time.sleep(0.2)
        order.append("first-end")
        return "ok"

    def second():
        """Record start/end."""
        order.append("second-start")
        order.append("second-end")
        return "ok"

    engine, registry = _bare_engine(
        tmp_path,
        [_multi_tool_turn([("first", {}), ("second", {})]), _text_turn("done")],
    )
    registry.register(first, metadata=medium)
    registry.register(second, metadata=medium)

    _collect(engine, "go")
    assert order == ["first-start", "first-end", "second-start", "second-end"]


class StreamingProvider(ProviderClient):
    def complete(self, **kwargs):  # pragma: no cover - streamed instead
        raise NotImplementedError

    def capabilities(self, model):
        return ModelCapabilities()

    def stream(self, *, model, messages, tools=None, **settings):
        for piece in ["Hel", "lo, ", "world"]:
            yield StreamChunk(text_delta=piece)
        yield StreamChunk(turn=AssistantTurn(text="Hello, world", finish_reason="stop"))


def test_streaming_emits_deltas(tmp_path):
    registry = ToolRegistry()
    permissions = PermissionEngine(workspace_root=tmp_path)
    engine = TurnEngine(
        provider=StreamingProvider(),
        registry=registry,
        permissions=permissions,
        model="gpt-5.5",
    )
    events = _collect(engine, "say hi")
    deltas = [e.data["text"] for e in events if e.type == EventType.ASSISTANT_DELTA]
    assert deltas == ["Hel", "lo, ", "world"]
    final = next(e for e in events if e.type == EventType.ASSISTANT_MESSAGE)
    assert final.data["text"] == "Hello, world"
    assert events[-1].type == EventType.TURN_END


def _pdf_file_part():
    import base64
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    url = "data:application/pdf;base64," + base64.b64encode(buf.getvalue()).decode()
    return {"type": "file", "file": {"filename": "d.pdf", "file_data": url}}


def test_outbound_adapts_pdf_for_non_pdf_models(tmp_path):
    # ScriptedProvider reports default caps (pdf=False) → the file part must be
    # replaced at send time while the stored history keeps the real document.
    engine, _ = _engine(tmp_path, [_text_turn("ok")])
    engine.messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": "read this"}, _pdf_file_part()],
        }
    )
    parts = engine._outbound_messages()[-1]["content"]
    assert all(p["type"] != "file" for p in parts)
    assert "d.pdf" in parts[-1]["text"]
    assert engine.messages[-1]["content"][1]["type"] == "file"  # history untouched


def test_outbound_keeps_pdf_for_native_models(tmp_path):
    class NativeProvider(ScriptedProvider):
        def capabilities(self, model):
            return ModelCapabilities(vision=True, pdf=True)

    engine, _ = _engine(tmp_path, [_text_turn("ok")])
    engine.provider = NativeProvider([_text_turn("ok")])
    message = {
        "role": "user",
        "content": [{"type": "text", "text": "read this"}, _pdf_file_part()],
    }
    engine.messages.append(message)
    assert engine._outbound_messages()[-1]["content"][1]["type"] == "file"


def test_provider_extras_persist_on_message_and_survive_outbound(tmp_path):
    """A turn's provider-private sidecar (`extras`, e.g. Gemini thought signatures) rides
    the persisted assistant message and is NOT stripped by _outbound_messages — the owning
    provider needs it back; foreign providers strip it themselves."""
    turn = AssistantTurn(
        text="ok",
        finish_reason="stop",
        extras={"_gemini": {"text_sig": "c2ln", "call_sigs": []}},
    )
    engine, _ = _engine(tmp_path, [turn])
    _collect(engine, "hi")

    persisted = engine.messages[-1]
    assert persisted["_gemini"] == {"text_sig": "c2ln", "call_sigs": []}
    outbound = engine._outbound_messages()[-1]
    assert outbound["_gemini"] == {"text_sig": "c2ln", "call_sigs": []}
    assert "ts" not in outbound  # display sidecars still stripped


def test_switch_model_appends_notice_only_midsession(tmp_path):
    engine, _ = _engine(tmp_path, [_text_turn("ok")])
    # Fresh session: first bind is silent.
    assert engine.switch_model("zai:glm-5.2") is None
    assert engine.model == "zai:glm-5.2"
    _collect(engine, "hi")
    # Same model: no-op.
    assert engine.switch_model("zai:glm-5.2") is None
    # Real mid-session switch: persisted marker with the matrix label.
    text = engine.switch_model("kimi:kimi-k2.6")
    assert "Kimi K2.6" in text and engine.model == "kimi:kimi-k2.6"
    notice = engine.messages[-1]
    assert notice["role"] == "notice" and notice["kind"] == "model_switch"
    assert all(m.get("role") != "notice" for m in engine._outbound_messages())


def test_switch_model_warns_when_images_meet_text_only_model(tmp_path):
    class NoVisionProvider(ScriptedProvider):
        def capabilities(self, model):
            return ModelCapabilities(vision=False)

    engine, _ = _engine(tmp_path, [_text_turn("ok")])
    engine.provider = NoVisionProvider([_text_turn("ok")])
    engine.messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ],
        }
    )
    text = engine.switch_model("zai:glm-5.2")
    assert "images" in text  # degradation is called out in the marker


def test_outbound_replaces_images_for_non_vision_models(tmp_path):
    class NoVisionProvider(ScriptedProvider):
        def capabilities(self, model):
            return ModelCapabilities(vision=False)

    engine, _ = _engine(tmp_path, [_text_turn("ok")])
    engine.provider = NoVisionProvider([_text_turn("ok")])
    engine.messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ],
        }
    )
    content = engine._outbound_messages()[-1]["content"]
    # All-text parts collapse back to a plain string: text-only providers'
    # OpenAI-compatible endpoints (DeepSeek & co.) reject or hang on array
    # `content` bodies (owner bug 2026-08-18: image+text send → silent LLM).
    assert isinstance(content, str)
    assert "image_url" not in content
    assert "not viewable" in content
    assert "look" in content
    assert engine.messages[-1]["content"][1]["type"] == "image_url"  # history untouched


def test_outbound_keeps_parts_for_vision_models(tmp_path):
    """A vision-capable model keeps the native image_url part (array body)."""

    class VisionProvider(ScriptedProvider):
        def capabilities(self, model):
            return ModelCapabilities(vision=True)

    engine, _ = _engine(tmp_path, [_text_turn("ok")])
    engine.provider = VisionProvider([_text_turn("ok")])
    engine.messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ],
        }
    )
    parts = engine._outbound_messages()[-1]["content"]
    assert isinstance(parts, list)
    assert any(p.get("type") == "image_url" for p in parts)


class StallProvider(ProviderClient):
    """Streams NOTHING (wedged gateway) — the stall guard must abort the turn."""

    def complete(self, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def capabilities(self, model):
        return ModelCapabilities()

    def stream(self, *, model, messages, tools=None, **settings):
        import time

        time.sleep(30)  # never yields — simulates a hung gateway
        yield StreamChunk(turn=AssistantTurn(text="late", finish_reason="stop"))
        return


def test_stalled_stream_aborts_turn_with_error(tmp_path, monkeypatch):
    """A provider that streams nothing must not hold the turn forever (owner-hit
    2026-08-10: a scheduled run parked 16+ minutes on a wedged gateway). The
    stall guard raises → ERROR event + retriable error notice."""
    import coworker.engine as engine_mod

    monkeypatch.setattr(engine_mod, "_STREAM_STALL_TIMEOUT", 0.5)
    monkeypatch.setattr(engine_mod, "_STREAM_TOTAL_TIMEOUT", 2.0)
    registry = ToolRegistry()
    permissions = PermissionEngine(workspace_root=tmp_path)
    engine = TurnEngine(
        provider=StallProvider(),
        registry=registry,
        permissions=permissions,
        model="gpt-5.5",
    )
    events = _collect(engine, "please respond")
    errs = [e for e in events if e.type == EventType.ERROR]
    assert errs, "expected an ERROR event after the stall"
    assert "no response" in str(errs[0].data["error"])
    # the error notice tail makes the turn retriable, not stuck
    assert engine._tail_is_retriable_error()


# -- prefix-continuity persistence (P0 命中率优化) ------------------------------

def test_persist_callback_fires_per_iteration(tmp_path):
    """A mid-run transcript sync: the persist callback is invoked after every
    model round (tool iterations), so a rebuilt engine loads full history and
    the provider prefix cache stays warm across a WS reconnect/restart."""
    calls = {"n": 0}

    def _persist():
        calls["n"] += 1

    provider = ScriptedProvider(
        [
            _tool_turn("write_file", {"path": "a.txt", "content": "x"}),
            _text_turn("done"),
        ]
    )
    registry = ToolRegistry()
    registry.register_all(ai.toolkits.files(root=str(tmp_path), allow_write=True))
    permissions = PermissionEngine(workspace_root=tmp_path)
    engine = TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model="gpt-5.5",
        persist_callback=_persist,
        max_iterations=8,
    )
    events = _collect(engine, "do it")
    assert any(ev.type.value == "turn_end" for ev in events)
    # one persist after the tool round + one after the final answer round
    assert calls["n"] >= 2
    # the transcript is complete (user + assistant tool-call + tool result + final)
    roles = [m.get("role") for m in engine.messages if m.get("role") != "notice"]
    assert "user" in roles and "tool" in roles


# -- usage fallback estimation (provider without include_usage) ---------------

def test_usage_estimated_when_provider_silent(tmp_path):
    """A provider that reports no usage must still move the monitor: the sink
    receives a character-based estimate instead of being silently skipped."""
    captured: dict = {}

    def _sink(entry):
        captured.update(entry)

    provider = ScriptedProvider(
        [
            _text_turn("你好世界" + "x" * 120),  # CJK + Latin, no usage reported
        ]
    )
    registry = ToolRegistry()
    registry.register_all(ai.toolkits.files(root=str(tmp_path), allow_write=True))
    permissions = PermissionEngine(workspace_root=tmp_path)
    engine = TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model="gpt-5.5",
        usage_sink=_sink,
        max_iterations=8,
    )
    _collect(engine, "hi")
    assert "prompt_tokens" in captured
    assert captured["completion_tokens"] >= 1
    assert captured["model"] == "gpt-5.5"


def test_estimate_usage_cjk_vs_latin():
    """CJK ≈ 1.5 char/token, Latin ≈ 4 char/token — a CJK-heavy completion
    estimates more tokens than the same character count in Latin."""
    from coworker.engine import TurnEngine

    provider = ScriptedProvider([_text_turn("ok")])
    registry = ToolRegistry()
    permissions = PermissionEngine(workspace_root=__import__("pathlib").Path("."))
    engine = TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model="gpt-5.5",
    )
    # A turn whose text is 30 CJK chars vs 30 Latin chars
    cjk = engine._estimate_usage(_text_turn("蜂群协作" * 7 + "数据"))  # ~30 CJK chars
    latin = engine._estimate_usage(_text_turn("a" * 120))
    assert cjk is not None and latin is not None
    # 30 CJK / 1.5 ≈ 20 tokens; 120 Latin / 4 = 30 tokens
    assert cjk["completion_tokens"] == 20
    assert latin["completion_tokens"] == 30


# -- usage_sink 透传回归 (build_engine → TurnEngine) ---------------------------

def test_build_engine_wires_usage_sink(tmp_path):
    """build_engine 必须把 usage_sink 传给 TurnEngine — 否则主会话/自动化
    的用量永不上报(用户反馈: 用量面板数据一直没变)。"""
    from coworker.agent import build_engine
    from coworker.agents import chat_agent

    calls: list[dict] = []

    def _sink(entry):
        calls.append(entry)

    engine = build_engine(
        agent=chat_agent(),
        workspace=str(tmp_path / "ws"),
        provider=ScriptedProvider([_text_turn("hi")]),
        usage_sink=_sink,
    )
    assert engine.usage_sink is _sink  # 透传成功
    # 跑一轮 → _record_usage 实际调用 sink
    _collect(engine, "hello")
    assert len(calls) >= 1
    assert "prompt_tokens" in calls[0]


# -- P0 结构无损裁剪 (GuaAgent 研究文档 #112) engine 集成 -----------------------

def test_outbound_trims_tool_output_but_keeps_user_and_assistant_verbatim(tmp_path):
    """结构无损裁剪: 超大 tool 输出被裁剪; user/assistant 消息原文 100% 保留。"""
    from coworker.trim import TOOL_TRIM_MIN_LEN

    engine, _ = _engine(tmp_path, [_text_turn("ok")])
    big = "D" * (TOOL_TRIM_MIN_LEN * 4)
    engine.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "用户的原始问题：请保留我。"},
        {
            "role": "assistant",
            "content": "助手原始回复：好的，我保留。",
            "tool_calls": [
                {"id": "call_x", "type": "function", "function": {"name": "run_shell", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_x", "content": big},
    ]
    out = engine._outbound_messages()
    # user / assistant 原文逐字保留
    assert out[1]["content"] == "用户的原始问题：请保留我。"
    assert out[2]["content"] == "助手原始回复：好的，我保留。"
    # tool 输出被裁剪
    tool = out[3]
    assert tool["role"] == "tool" and tool["tool_call_id"] == "call_x"
    assert "trimmed" in tool["content"] and len(tool["content"]) < len(big)
    # 持久化历史保持完整输出 (结构无损 = 磁盘不丢)
    assert engine.messages[3]["content"] == big


def test_outbound_trims_base64_in_tool_output(tmp_path):
    engine, _ = _engine(tmp_path, [_text_turn("ok")])
    engine.messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_y", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_y", "content": "pic: data:image/png;base64," + "Q" * 3000},
    ]
    out = engine._outbound_messages()
    assert "base64," not in out[-1]["content"]
    assert "collapsed" in out[-1]["content"]


def test_outbound_keeps_short_tool_output_verbatim(tmp_path):
    engine, _ = _engine(tmp_path, [_text_turn("ok")])
    engine.messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_z", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_z", "content": "short result"},
    ]
    out = engine._outbound_messages()
    assert out[-1]["content"] == "short result"


def test_trim_disabled_restores_verbatim_feed(tmp_path):
    """trim_tool_outputs=False → provider feed 与旧行为逐字节一致。"""
    from coworker.trim import TOOL_TRIM_MIN_LEN

    engine, _ = _engine(tmp_path, [_text_turn("ok")])
    engine.trim_tool_outputs = False
    big = "E" * (TOOL_TRIM_MIN_LEN * 4)
    engine.messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_w", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_w", "content": big},
    ]
    out = engine._outbound_messages()
    assert out[-1]["content"] == big  # 未裁剪


def test_build_engine_wires_trim_switch_from_config(tmp_path, monkeypatch):
    """build_engine 从 config 读取 trim_tool_outputs 开关。"""
    from coworker.agent import build_engine
    from coworker.agents import chat_agent

    engine = build_engine(
        agent=chat_agent(),
        workspace=str(tmp_path / "ws"),
        provider=ScriptedProvider([_text_turn("hi")]),
    )
    assert engine.trim_tool_outputs is True  # 默认开启

    # config.toml 显式关闭 → 开关为 False
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / ".coworker").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / ".coworker" / "config.toml").write_text(
        "trim_tool_outputs = false\n", encoding="utf-8"
    )
    engine2 = build_engine(
        agent=chat_agent(),
        workspace=str(tmp_path / "ws"),
        provider=ScriptedProvider([_text_turn("hi")]),
    )
    assert engine2.trim_tool_outputs is False


def test_analyze_image_tool_runs_without_approval(tmp_path):
    """图+文字时模型调 analyze_image(读图) 应免审批直接执行 —— 修复 2026-08-18
    「图+文字发送 LLM 没反应」= 模型调 run_shell 读图卡在权限审批。"""
    from coworker.tools.vision import vision_tools

    engine, registry = _bare_engine(
        tmp_path,
        [
            _multi_tool_turn([("analyze_image", {"path": "x.png", "ocr_only": True})]),
            _text_turn("done"),
        ],
    )
    registry.register(*vision_tools())

    events = _collect(engine, "analyze this image")
    kinds = [e.type.value for e in events]
    assert "permission_required" not in kinds, (
        "analyze_image must be auto-approved — an approval prompt here is exactly the "
        "user-visible hang we fixed"
    )
    finished = [e for e in events if e.type == EventType.TOOL_FINISHED]
    assert len(finished) == 1
    assert finished[0].data["status"] == "ok"
    # 工具结果以 tool 消息回填, 下一轮模型可见
    assert any(m.get("role") == "tool" for m in engine.messages)
