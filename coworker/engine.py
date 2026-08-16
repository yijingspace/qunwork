"""TurnEngine — the owned agent loop.

Async, but with blocking provider/tool calls wrapped in `asyncio.to_thread` so the loop
(and any UI consuming its events) stays responsive. One user turn spans many model↔tool
iterations until the model stops requesting tools, a rail trips, or it's interrupted.
When the model requests several tool calls in one turn, low-risk ones (reads, searches)
execute concurrently; writes/shell stay strictly ordered.

Approvals are handled out-of-band via an injected async `approver`: when the permission
engine says `needs_user`, the engine emits `PERMISSION_REQUIRED` and awaits the approver.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from .events import Event, EventType
from .permissions import Mode, PermissionEngine
from .providers import AssistantTurn, ProviderClient, ToolCall
from .providers.errors import friendly_model_error
from .tools import ToolRegistry


class ApprovalOutcome(str, Enum):
    ONCE = "once"
    ALWAYS_TOOL = "always_tool"
    ALWAYS_COMMAND = "always_command"
    DENY = "deny"


@dataclass
class PermissionRequest:
    tool_name: str
    arguments: dict[str, Any]
    metadata: Any
    reason: str
    tool_call_id: Optional[str] = None  # for durable resume (idempotent inbox item)


Approver = Callable[[PermissionRequest], Awaitable[ApprovalOutcome]]


async def _deny_all(_request: PermissionRequest) -> ApprovalOutcome:
    return ApprovalOutcome.DENY


class TurnEngine:
    def __init__(
        self,
        *,
        provider: ProviderClient,
        registry: ToolRegistry,
        permissions: PermissionEngine,
        model: str,
        instructions: Optional[str] = None,
        approver: Optional[Approver] = None,
        max_iterations: int = 12,
        model_settings: Optional[dict[str, Any]] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        audit_sink: Optional[Callable[[dict[str, Any]], None]] = None,
        context_provider: Optional[Callable[[], str]] = None,
        directory_requester: Optional[
            Callable[[dict[str, Any]], "Awaitable[dict[str, Any]]"]
        ] = None,
        plan_approver: Optional[
            Callable[[dict[str, Any]], "Awaitable[dict[str, Any]]"]
        ] = None,
        question_asker: Optional[
            Callable[[dict[str, Any]], "Awaitable[dict[str, Any]]"]
        ] = None,
        # Called (thread-safe, best-effort) when the user stops the turn — e.g. the
        # executor's kill for a running shell command.
        interrupt_hooks: Optional[list[Callable[[], None]]] = None,
        # The session's skill-catalog loader (progressive disclosure). Held so surfaces can
        # refresh it when skills change mid-session (e.g. an API import); optional.
        skill_loader: Optional[Any] = None,
        # Per-turn token ledger sink (see usage_sink below). Optional.
        usage_sink: Optional[Callable[[dict[str, Any]], None]] = None,
        # Prefix-continuity persistence hook (P0 建议4 命中率优化): called after
        # every model iteration so the session history is durably synced mid-run.
        # A rebuilt engine (WS reconnect / durable resume / restart) then loads the
        # FULL transcript, the provider's prefix cache stays warm, and the next
        # round hits instead of rebuilding. Optional.
        persist_callback: Optional[Callable[[], None]] = None,
        # P1-5 零信任能力袋: persona scope 检查器 (可选)。传入后 _authorize 会在
        # 工具调用前检查 scope, 越权自动升级为审批。
        scope_store: Optional[Any] = None,
        # P0 结构无损裁剪: 发送给 provider 前, 对超大工具输出 / base64 数据 /
        # 元数据块做"结构无损剪枝"(GuaAgent 研究文档 #112), 平均省 ~20% token。
        # 只在 provider feed 上生效 —— 持久化的 self.messages 始终保留完整输出。
        trim_tool_outputs: bool = True,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.permissions = permissions
        self.model = model
        self.approver = approver or _deny_all
        self.max_iterations = max_iterations
        self.model_settings = dict(model_settings or {})
        self.messages: list[dict[str, Any]] = list(messages or [])
        self.audit_sink = audit_sink
        # Per-turn token ledger callback: called once per model turn with
        # {"session_id", "model", "prompt_tokens", "completion_tokens",
        # "cached_tokens", "cache_miss_tokens"} when the provider reports usage.
        self.usage_sink = usage_sink
        self.persist_callback = persist_callback
        self.scope_store = scope_store  # P1-5 PersonaScopeStore or None
        self.trim_tool_outputs = bool(trim_tool_outputs)  # P0 结构无损裁剪开关
        # Returns an ephemeral `<system-context>` block appended to the LAST user message at
        # send-time only (never persisted). We can't reliably inject system messages mid-thread
        # across providers, so dynamic per-turn context (e.g. the live directory list) rides on
        # the latest user turn. Returns "" when there's nothing to add.
        self.context_provider = context_provider
        # Handles the `request_directory` tool: emits a DIRECTORY_REQUESTED prompt, waits for the
        # user to grant/decline a folder out-of-band, applies the grant to this live session, and
        # returns the outcome. None on surfaces that can't prompt (the tool then no-ops).
        self.directory_requester = directory_requester
        # Handles the `propose_plan` tool: emits PLAN_PROPOSED, waits for the user's decision.
        # An approving result flips the live PermissionEngine out of plan mode (same session,
        # context kept). None on surfaces that can't prompt (the tool then no-ops).
        self.plan_approver = plan_approver
        # Handles the `ask_user` tool: turns a question into an Inbox item and waits for the answer
        # (answerable inline in a live session or from the Inbox when unattended). None on surfaces
        # that can't ask (the tool then no-ops).
        self.question_asker = question_asker
        self.skill_loader = skill_loader
        self.audit_context: dict[str, Any] = {}
        if instructions and not (
            self.messages and self.messages[0].get("role") == "system"
        ):
            self.messages.insert(0, {"role": "system", "content": instructions})
        self._cancel = asyncio.Event()
        # Each pending steering message: (text, optional MessageSource sidecar dict).
        self._steering: list[tuple[str, Optional[dict[str, Any]]]] = []
        # tool_call.id → the standing rule that auto-allowed it ("tool → target"), so the
        # TOOL_FINISHED event can carry the note to the tool card (§25).
        self._standing_notes: dict[str, str] = {}
        self._interrupt_hooks: list[Callable[[], None]] = list(interrupt_hooks or [])
        # 13 Agent 影子模式: 决策回放轨迹。每次工具选择/权限审批/scope 升级/路由抉择
        # 都追加一条 entry, 前端时间轴 UI 可拖动滑块回放「AI 在每一步看到了什么、
        # 考虑了哪些选项、为什么选了这个」。_iterations 由 run() 递增。
        self.decision_trace: list[dict[str, Any]] = []
        self._iterations = 0

    # -- external controls ------------------------------------------------------
    def request_interrupt(self) -> None:
        """Stop the turn as soon as possible, from ANY state: mid-stream (the producer
        thread drops the stream between chunks), mid-tool (interrupt hooks kill the
        running command), awaiting an approval/question/plan (the await resolves as
        interrupted), or between iterations (the loop checkpoint). Every pending
        tool_call still gets a tool-error result so the history never carries orphans
        (hosted templates reject them, and durable-resume would re-prompt them)."""
        # C6: `_cancel` is a loop-bound asyncio.Event — `.set()` from a foreign
        # thread (FastAPI sync endpoints run in a threadpool) is undefined
        # behavior. Route through the bound loop when we're off it.
        loop = getattr(self, "_event_loop", None)
        if loop is not None and loop.is_running():
            try:
                asyncio.get_running_loop()  # already on the loop thread?
                self._cancel.set()
            except RuntimeError:
                loop.call_soon_threadsafe(self._cancel.set)
        else:
            self._cancel.set()
        for hook in self._interrupt_hooks:
            try:
                hook()
            except Exception:
                pass  # best-effort: a dead executor must not block the stop

    async def _interruptible(self, coro: Any, interrupted: Any) -> Any:
        """Await `coro`, but resolve early with `interrupted` if the user stops the
        turn. The pending task is cancelled so an answered-later Inbox card no-ops."""
        task = asyncio.ensure_future(coro)
        cancel_wait = asyncio.ensure_future(self._cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                return task.result()
            task.cancel()
            return interrupted
        finally:
            cancel_wait.cancel()

    def queue_steering(
        self, text: str, source: Optional[dict[str, Any]] = None
    ) -> None:
        self._steering.append((text, source))

    # -- main loop --------------------------------------------------------------
    async def run(
        self, user_input: "str | list", *, source: Optional[dict[str, Any]] = None
    ) -> AsyncIterator[Event]:
        # `user_input` is a string, or OpenAI content-parts (text + image_url) for attachments.
        # `source` (a MessageSource dict) is a display-only sidecar for connector messages: it
        # rides on the persisted user message + the TURN_START event, but is stripped before the
        # message reaches a provider (see `_outbound_messages`). `content` stays the framed text.
        # `ts` (unix seconds, stamped on every appended message) is the same kind of sidecar.
        message: dict[str, Any] = {
            "role": "user",
            "content": user_input,
            "ts": time.time(),
        }
        if source is not None:
            message["source"] = source
        self.messages.append(message)
        # C6: bind the owning loop so request_interrupt (callable from FastAPI
        # threadpool threads) can route Event.set() through call_soon_threadsafe.
        self._event_loop = asyncio.get_running_loop()
        self._cancel.clear()
        data: dict[str, Any] = {"input": user_input}
        if source is not None:
            data["source"] = source
        yield Event(EventType.TURN_START, data)
        async for event in self._loop():
            yield event

    def switch_model(self, model: str) -> Optional[str]:
        """Rebind the session's model mid-conversation (roadmap item 3). History is
        canonical OpenAI shape and every provider converts per call, so the switch is just
        the field write — plus a persisted notice marking WHERE it happened, with a
        degradation warning when history carries images the new model can't see (those are
        sent as placeholders — see `_outbound_messages`). Returns the notice text, or None
        when nothing changed (same model, or first bind on a fresh session)."""
        if not model or model == self.model:
            return None
        had_history = any(m.get("role") != "system" for m in self.messages)
        self.model = model
        if not had_history:
            return None
        from .providers.matrix import model_labels

        text = f"Model switched to {model_labels().get(model, model)}"
        try:
            caps = self.provider.capabilities(model)
        except Exception:
            caps = None
        if (
            caps is not None
            and not getattr(caps, "vision", False)
            and self._history_has_images()
        ):
            text += " — earlier images can't be read by this model"
        self._append_notice("model_switch", text)
        return text

    def _history_has_images(self) -> bool:
        return any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for msg in self.messages
            if isinstance(msg.get("content"), list)
            for p in msg["content"]
        )

    def _tail_is_retriable_error(self) -> bool:
        """True when the history tail is an error notice, looking through any model_switch
        notices appended after it (a switch must not consume the retry)."""
        for message in reversed(self.messages):
            if message.get("role") != "notice":
                return False
            if message.get("kind") == "model_switch":
                continue
            return message.get("kind") == "error"
        return False

    def _persist(self) -> None:
        """Durably sync the transcript after a model round (best-effort). A rebuilt
        engine then loads the full history and the provider prefix cache stays warm."""
        if self.persist_callback is not None:
            try:
                self.persist_callback()
            except Exception:
                pass  # persistence must never break the turn

    def _append_notice(self, kind: str, text: Optional[str] = None) -> None:
        """Persist a turn-ending marker (error/interrupted) as a display-only `notice`
        message: it survives reload like the transcript does, but `_outbound_messages`
        drops the role so no provider ever sees it."""
        notice: dict[str, Any] = {"role": "notice", "kind": kind, "ts": time.time()}
        if text:
            notice["text"] = text
        self.messages.append(notice)

    async def retry(self) -> AsyncIterator[Event]:
        """Re-run the model loop after a provider error — no new user message; the failed
        turn's input is already the tail of history. Guarded on the tail being an error
        notice so a stray retry frame can't re-answer a completed turn. Trailing
        model_switch notices don't break the guard — switching models and THEN retrying
        is the intended recovery path (owner-hit 2026-07-23)."""
        if not self._tail_is_retriable_error():
            return
        self._cancel.clear()
        yield Event(EventType.TURN_START, {"input": ""})
        async for event in self._loop():
            yield event

    async def resume(self) -> AsyncIterator[Event]:
        """Continue a turn that was suspended at a prompt and persisted — durable resume after a
        restart (or engine eviction). Re-process the trailing assistant message's UNANSWERED
        tool-calls (the prompt callbacks find the already-resolved Inbox item and return without
        re-prompting; answered calls are skipped, so nothing double-executes), then run the model
        loop to finish the turn."""
        pending = self._unanswered_trailing_tool_calls()
        if not pending:
            return
        self._cancel.clear()
        yield Event(EventType.TURN_START, {"input": "(resumed)"})
        async for event in self._handle_tool_calls(pending):
            yield event
        yield Event(EventType.ITERATION_END, {"iteration": 0})
        self._persist()
        if not self._cancel.is_set():
            async for event in self._loop():
                yield event

    def _unanswered_trailing_tool_calls(self) -> list[ToolCall]:
        """The tool-calls of the last assistant message that don't yet have a tool result —
        i.e. the prompt we suspended on (+ any after it). Reconstructed from the persisted thread.
        """
        answered = {
            m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"
        }
        for msg in reversed(self.messages):
            if msg.get("role") == "user":
                return []
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                out: list[ToolCall] = []
                for tc in msg["tool_calls"]:
                    if tc.get("id") in answered:
                        continue
                    fn = tc.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    out.append(
                        ToolCall(id=tc.get("id"), name=fn.get("name"), arguments=args)
                    )
                return out
        return []

    async def _loop(self) -> AsyncIterator[Event]:
        iterations = 0
        while True:
            if iterations >= self.max_iterations:
                yield Event(
                    EventType.TURN_END,
                    {"status": "max_iterations_exceeded", "iterations": iterations},
                )
                return
            iterations += 1
            self._iterations = iterations  # 13 影子模式: 同步给 _record_decision

            turn: Optional[AssistantTurn] = None
            streamed: list[str] = []
            streamed_reasoning: list[str] = []

            def _partial_turn() -> AssistantTurn:
                # What the user watched arrive — text and thinking, NO tool calls (any
                # half-formed calls would either orphan or execute against the stop).
                return AssistantTurn(
                    text="".join(streamed) or None,
                    reasoning="".join(streamed_reasoning) or None,
                )

            try:
                async for chunk in self._astream():
                    if chunk.reasoning_delta:
                        streamed_reasoning.append(chunk.reasoning_delta)
                        yield Event(
                            EventType.REASONING_DELTA, {"text": chunk.reasoning_delta}
                        )
                    if chunk.text_delta:
                        streamed.append(chunk.text_delta)
                        yield Event(
                            EventType.ASSISTANT_DELTA, {"text": chunk.text_delta}
                        )
                    if chunk.turn is not None:
                        turn = chunk.turn
            except Exception as exc:  # provider failure
                # Same contract as the stop path below: the partial the user watched
                # arrive survives the failure.
                if streamed or streamed_reasoning:
                    self.messages.append(_assistant_message(_partial_turn()))
                friendly = friendly_model_error(self.model, exc)
                payload = {
                    "error": friendly or str(exc),
                    "error_type": type(exc).__name__,
                }
                if friendly:
                    payload["raw"] = str(exc)
                self._append_notice("error", friendly or str(exc))
                yield Event(EventType.ERROR, payload)
                return
            if self._cancel.is_set() and turn is None:
                # Stopped mid-stream: persist exactly what the user watched arrive.
                if streamed or streamed_reasoning:
                    self.messages.append(_assistant_message(_partial_turn()))
                self._append_notice("interrupted")
                yield Event(EventType.INTERRUPTED, {"iterations": iterations})
                return
            if turn is None:
                turn = AssistantTurn()

            self.messages.append(_assistant_message(turn))
            self._record_usage(turn)
            payload: dict[str, Any] = {
                "text": turn.text,
                "tool_calls": [tc.name for tc in turn.tool_calls],
            }
            if turn.reasoning:
                payload["reasoning"] = turn.reasoning
            yield Event(EventType.ASSISTANT_MESSAGE, payload)

            if not turn.tool_calls:
                if self._steering:
                    self._inject_steering()
                    continue
                self._persist()  # final round: sync before TURN_END
                yield Event(
                    EventType.TURN_END,
                    {"status": "completed", "iterations": iterations},
                )
                return

            async for event in self._handle_tool_calls(turn.tool_calls):
                yield event

            yield Event(EventType.ITERATION_END, {"iteration": iterations})
            # Prefix-continuity: sync the full transcript after every model round so
            # a mid-run engine rebuild loads the complete history (provider prefix
            # cache stays warm — no 100% miss round after a WS reconnect/restart).
            self._persist()

            if self._cancel.is_set():
                self._append_notice("interrupted")
                yield Event(EventType.INTERRUPTED, {"iterations": iterations})
                return
            if self._steering:
                self._inject_steering()

    # -- helpers ----------------------------------------------------------------
    async def _astream(self):
        """Bridge the provider's blocking stream generator to the async loop via a
        thread + queue, so text deltas surface live without blocking the event loop."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        tools = self.registry.schemas() or None
        model, messages, settings = (
            self.model,
            self._outbound_messages(),
            self.model_settings,
        )
        provider = self.provider
        # Stall guards for ONE model call (owner-hit 2026-08-10 — a scheduled run
        # parked 16+ minutes with zero chunks: a wedged gateway held the turn
        # forever because the SDK's default timeout does not apply to streaming
        # iteration). DeepSeek reasoning streams still emit `reasoning` chunks,
        # so 90s of absolute silence means the connection is dead, not thinking.
        # The 600s ceiling is the backstop for a slow-but-alive stream.
        deadline = loop.time() + _STREAM_TOTAL_TIMEOUT

        def produce():
            try:
                for chunk in provider.stream(
                    model=model, messages=messages, tools=tools, **settings
                ):
                    # User pressed Stop: drop the stream between chunks (reading the
                    # asyncio.Event's flag from a thread is safe; we only read).
                    if self._cancel.is_set():
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
            except Exception as exc:  # surfaced to the awaiting consumer
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        loop.run_in_executor(None, produce)
        got_any = False
        try:
            while True:
                # Race the queue against Stop so a stalled stream (no chunks arriving —
                # the pre-first-token wait, a wedged connection) can't hold the turn.
                get_task = asyncio.ensure_future(queue.get())
                cancel_task = asyncio.ensure_future(self._cancel.wait())
                remaining = deadline - loop.time()
                if remaining <= 0:
                    get_task.cancel()
                    cancel_task.cancel()
                    raise TimeoutError(f"model call exceeded {_STREAM_TOTAL_TIMEOUT:.0f}s")
                # No output yet: a stall deadline. After the first chunk the total
                # deadline alone applies (long-thinking models can pause between chunks).
                stall = _STREAM_STALL_TIMEOUT if not got_any else remaining
                done, _ = await asyncio.wait(
                    {get_task, cancel_task},
                    timeout=min(remaining, stall),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                cancel_task.cancel()
                if get_task not in done:
                    get_task.cancel()
                    if self._cancel.is_set():
                        return  # user pressed Stop — the producer exits on its own
                    if not got_any:
                        raise TimeoutError(
                            f"no response from model within {_STREAM_STALL_TIMEOUT:.0f}s"
                        )
                    raise TimeoutError("model stream stalled (no chunks)")
                kind, payload = get_task.result()
                if kind == "chunk":
                    got_any = True
                    yield payload
                elif kind == "error":
                    raise payload
                else:
                    return
        finally:
            pass  # C5 temporarily disabled for bisect

    async def _handle_tool_calls(
        self, tool_calls: list[ToolCall]
    ) -> AsyncIterator[Event]:
        """Run one assistant turn's tool calls: authorize all of them first (sequentially —
        approval prompts are interactive), then execute. Low-risk calls (reads, searches)
        run concurrently; everything else runs one at a time in call order."""
        # 13 影子模式: 记录「工具选择」决策 — 模型本轮可选的全部工具 + 它实际选的 Top-K
        # + 选择理由 (来自 assistant 的 reasoning)。这是企业合规回放的核心证据。
        self._record_decision(
            "tool_selection",
            available_tools=list(self.registry.names()),
            candidates=[
                {"name": tc.name, "arguments": tc.arguments}
                for tc in tool_calls
            ],
            choice=[tc.name for tc in tool_calls],
            reason="model emitted these tool_calls in this assistant turn",
        )
        cleared: list[ToolCall] = []
        for tool_call in tool_calls:
            if self._cancel.is_set():
                # Stopped: every remaining call still gets an answer (no orphans).
                yield self._interrupted_tool(tool_call)
                continue
            yield Event(
                EventType.TOOL_PROPOSED,
                {"name": tool_call.name, "arguments": tool_call.arguments},
            )
            self._audit(tool_call, stage="proposed")
            # `request_directory` and `propose_plan` are interactive: the user decides
            # out-of-band and that decision IS the consent, so they skip the
            # permission/registry path.
            if tool_call.name == "request_directory":
                async for event in self._handle_directory_request(tool_call):
                    yield event
                continue
            if tool_call.name == "propose_plan":
                async for event in self._handle_plan_proposal(tool_call):
                    yield event
                continue
            if tool_call.name == "ask_user":
                async for event in self._handle_ask_user(tool_call):
                    yield event
                continue
            allowed = False
            async for item in self._authorize(tool_call):
                if isinstance(item, Event):
                    yield item
                else:
                    allowed = item
            if allowed:
                cleared.append(tool_call)

        concurrent = (
            [tc for tc in cleared if self._parallel_safe(tc)]
            if len(cleared) > 1
            else []
        )
        serial = [tc for tc in cleared if tc not in concurrent]

        if concurrent:
            for tool_call in concurrent:
                yield Event(EventType.TOOL_STARTED, {"name": tool_call.name})
                self._audit(tool_call, stage="started")
            outcomes = await asyncio.gather(
                *[asyncio.to_thread(self._execute_sync, tc) for tc in concurrent]
            )
            for tool_call, (result, status) in zip(concurrent, outcomes):
                yield self._record_result(tool_call, result, status)

        for tool_call in serial:
            if self._cancel.is_set():
                yield self._interrupted_tool(tool_call)
                continue
            yield Event(EventType.TOOL_STARTED, {"name": tool_call.name})
            self._audit(tool_call, stage="started")
            result, status = await asyncio.to_thread(self._execute_sync, tool_call)
            yield self._record_result(tool_call, result, status)

    def _interrupted_tool(self, tool_call: ToolCall) -> Event:
        """The stop-path answer for a call that will not run: a tool-error result in the
        history (hosted chat templates reject orphaned tool_calls, and durable-resume
        would otherwise re-prompt it) + the finished event for the tool card."""
        self.messages.append(_tool_error_message(tool_call, "interrupted by user"))
        self._audit(
            tool_call, stage="finished", status="interrupted", reason="user stop"
        )
        return Event(
            EventType.TOOL_FINISHED,
            {"name": tool_call.name, "status": "interrupted", "reason": "stopped"},
        )

    def _parallel_safe(self, tool_call: ToolCall) -> bool:
        # Only metadata-declared low-risk tools (reads, searches, git queries) run
        # concurrently; writes, shell, and anything unannotated stay strictly ordered.
        spec = self.registry.get(tool_call.name)
        metadata = spec.metadata if spec else None
        return getattr(metadata, "risk_level", "") == "low" and not getattr(
            metadata, "requires_approval", False
        )

    async def _authorize(self, tool_call: ToolCall) -> "AsyncIterator[Event | bool]":
        """Permission flow for one call (TOOL_PROPOSED is emitted by the caller). Yields
        its events, then True/False (allowed) last. Denied/unknown calls get their
        tool-error message appended here."""
        from .permissions import standing_rule_candidate

        spec = self.registry.get(tool_call.name)
        metadata = spec.metadata if spec else None

        decision = self.permissions.evaluate(
            tool_call.name, tool_call.arguments, metadata
        )
        allowed = decision.allowed
        reason = decision.reason
        scope_needs_approval = False  # P1-5: scope 越权 → 升级审批

        # 13 影子模式: 记录权限评估决策 (RBAC 层)。
        self._record_decision(
            "permission",
            tool=tool_call.name,
            allowed=allowed,
            reason=reason,
            needs_user=decision.needs_user,
            rule=decision.rule or "",
        )

        # P1-5 零信任能力袋: 在常规权限通过后, 再检查 connector scope。
        # 如果 persona 没有被授予该工具所需的 scope, 自动升级为审批。
        if allowed and not decision.needs_user and self.scope_store is not None:
            from .connector_scopes import check_scope
            persona_id = self.audit_context.get("agent") or "default"
            scope_dec = check_scope(persona_id, tool_call.name, self.scope_store)
            if not scope_dec.allowed and scope_dec.needs_approval:
                allowed = False
                reason = f"[scope] {scope_dec.reason}"
                scope_needs_approval = True
                self._audit(
                    tool_call,
                    stage="scope_escalation",
                    status="needs_approval",
                    reason=scope_dec.reason,
                    connector=scope_dec.connector or "",
                    required_scopes=",".join(scope_dec.required_scopes),
                )
                # 13 影子模式: 记录 scope 越权升级 (零信任层)。
                self._record_decision(
                    "scope_escalation",
                    tool=tool_call.name,
                    persona=persona_id,
                    connector=scope_dec.connector or "",
                    required_scopes=scope_dec.required_scopes,
                    granted_scopes=scope_dec.granted_scopes,
                    reason=scope_dec.reason,
                )

        if allowed and decision.rule:
            # A task-scoped standing rule auto-allowed this call: audit the exact rule
            # (§25 invariant — every auto-allowed call cites its rule) and remember it so
            # the tool card can say "allowed by standing rule".
            self._standing_notes[tool_call.id] = decision.rule
            self._audit(
                tool_call, stage="auto_allowed", status="allowed", reason=reason
            )

        if not allowed and (decision.needs_user or scope_needs_approval):
            yield Event(
                EventType.PERMISSION_REQUIRED,
                {
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                    "reason": reason,
                    "category": getattr(metadata, "category", ""),
                    # The exact target a standing rule could pin, or None when the call
                    # isn't eligible (no declared target arg / exec risk). Surfaces use it
                    # to offer "Allow every time" on automation-run approval cards only.
                    "standing_target": standing_rule_candidate(
                        tool_call.name,
                        tool_call.arguments,
                        metadata,
                        self.permissions.risk_overrides,
                    ),
                },
            )
            self._audit(tool_call, stage="approval_requested", reason=reason)
            outcome = await self._interruptible(
                self.approver(
                    PermissionRequest(
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                        metadata=metadata,
                        reason=reason,
                        tool_call_id=tool_call.id,
                    )
                ),
                interrupted=ApprovalOutcome.DENY,
            )
            if outcome is ApprovalOutcome.DENY:
                allowed, reason = (
                    False,
                    "interrupted by user" if self._cancel.is_set() else "denied by user",
                )
                self._audit(
                    tool_call,
                    stage="approval_resolved",
                    status="denied",
                    approval=outcome.value,
                    reason=reason,
                )
                # 13 影子模式: 记录审批解决 (拒绝/中断)。
                self._record_decision(
                    "approval_resolution",
                    tool=tool_call.name,
                    outcome=outcome.value,
                    allowed=False,
                    reason=reason,
                )
            else:
                if outcome is ApprovalOutcome.ALWAYS_TOOL:
                    self.permissions.allow_tool_for_session(tool_call.name)
                elif outcome is ApprovalOutcome.ALWAYS_COMMAND:
                    self.permissions.allow_command_for_session(
                        str(tool_call.arguments.get("command", ""))
                    )
                allowed, reason = True, "approved by user"
                self._audit(
                    tool_call,
                    stage="approval_resolved",
                    status="approved",
                    approval=outcome.value,
                    reason=reason,
                )
                # 13 影子模式: 记录审批解决 (批准)。
                self._record_decision(
                    "approval_resolution",
                    tool=tool_call.name,
                    outcome=outcome.value,
                    allowed=True,
                    reason=reason,
                )

        if not allowed:
            if spec is None:
                reason = f"unknown tool: {tool_call.name}"
            self.messages.append(_tool_error_message(tool_call, reason))
            yield Event(
                EventType.TOOL_FINISHED,
                {"name": tool_call.name, "status": "denied", "reason": reason},
            )
            self._audit(tool_call, stage="finished", status="denied", reason=reason)
            yield False
            return

        if spec is None:
            self.messages.append(
                _tool_error_message(tool_call, f"unknown tool: {tool_call.name}")
            )
            yield Event(
                EventType.TOOL_FINISHED,
                {"name": tool_call.name, "status": "error", "reason": "unknown tool"},
            )
            yield False
            return

        yield True

    def _execute_sync(self, tool_call: ToolCall) -> tuple[Any, str]:
        """Execute one authorized call (runs in a worker thread)."""
        try:
            return self.registry.execute(tool_call.name, tool_call.arguments), "ok"
        except Exception as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}, "error"

    def _record_result(self, tool_call: ToolCall, result: Any, status: str) -> Event:
        # A `_display` key on a tool result is user-facing metadata the AGENT must
        # never see (e.g. how many gmail hits the privacy filters hid — a count
        # the model could probe around). Lift it onto the message as a sidecar
        # (like `source`), stripped from every provider feed in
        # `_outbound_messages` but persisted for the GUI's tool card.
        display: Optional[dict[str, Any]] = None
        if isinstance(result, dict) and "_display" in result:
            display = result.get("_display") or None
            result = {k: v for k, v in result.items() if k != "_display"}
        message = _tool_result_message(tool_call, result)
        if display:
            message["_display"] = display
        self.messages.append(message)
        hidden = int((display or {}).get("hidden_by_filters") or 0)
        stripped = int((display or {}).get("hidden_fields") or 0)
        if hidden or stripped:
            # The out-of-band trace the user CAN see: rule class + count, never content.
            parts = []
            if hidden:
                parts.append(f"{hidden} result(s) hidden")
            if stripped:
                parts.append(f"{stripped} field value(s) stripped")
            self._audit(
                tool_call,
                stage="filtered",
                status="hidden",
                reason=" · ".join(parts) + " by privacy filters",
            )
        self._audit(
            tool_call,
            stage="finished",
            status=status,
            result=result,
            result_preview=_preview(result),
        )
        rule = self._standing_notes.pop(tool_call.id, "")
        return Event(
            EventType.TOOL_FINISHED,
            {
                "name": tool_call.name,
                "status": status,
                "result_preview": _preview(result),
                **({"display": display} if display else {}),
                **({"standing_rule": rule} if rule else {}),
            },
        )

    def _record_usage(self, turn: AssistantTurn) -> None:
        """Push the turn's token usage to the ledger sink. Falls back to a
        character-based estimate when the provider didn't report usage so the
        monitor keeps moving even on servers without include_usage support."""
        if self.usage_sink is None:
            return
        usage = turn.usage or self._estimate_usage(turn)
        if not usage:
            return
        try:
            self.usage_sink(
                {
                    **self.audit_context,
                    "model": self.model,
                    **usage,
                }
            )
        except Exception:
            pass

    def _estimate_usage(self, turn: AssistantTurn) -> Optional[dict[str, int]]:
        """Conservative per-character estimate (CJK ≈ 1.5 char/token, Latin ≈ 4
        char/token). Used only when the provider reported no usage at all, so
        the usage panel never stays frozen at 0."""
        text = (turn.text or "") + (turn.reasoning or "")
        for tc in turn.tool_calls:
            args = tc.arguments if isinstance(tc.arguments, str) else str(tc.arguments)
            text += (tc.name or "") + args
        if not text:
            return None
        def _cjk_count(s: str) -> int:
            return sum(
                1
                for ch in s
                if "\u3000" <= ch <= "\u9fff"
                or "\uac00" <= ch <= "\ud7af"
                or "\u0e00" <= ch <= "\u0fff"
            )
        cjk = _cjk_count(text)
        latin = len(text) - cjk
        completion = max(1, round(cjk / 1.5 + latin / 4))
        # For prompt we don't have the raw request here; _record_usage is
        # called AFTER messages.append, so we can ballpark from the last few
        # message bodies already in the engine. Good enough for a fallback
        # monitor, and any real provider returns exact include_usage anyway.
        recent_chars = 0
        for m in self.messages[-3:]:
            c = m.get("content") or ""
            if isinstance(c, str):
                recent_chars += len(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        recent_chars += len(part["text"])
        cjk_p = _cjk_count(text)
        prompt = max(1, round(cjk_p / 1.5 + max(0, recent_chars - cjk_p) / 4))
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_tokens": 0,
            "cache_miss_tokens": prompt,
        }

    def _audit(self, tool_call: ToolCall, **event: Any) -> None:
        if self.audit_sink is None:
            return
        payload = {
            **self.audit_context,
            "tool": tool_call.name,
            "arguments": tool_call.arguments,
            **event,
        }
        try:
            self.audit_sink(payload)
        except Exception:
            pass

    # -- 13 Agent 影子模式: 决策回放 -------------------------------------------
    # 敏感参数脱敏: 工具参数可能含 write_file 内容 / API 密钥类字段 —
    # 决策轨迹(内存 + audit 镜像 + HTTP 出口)不得以明文携带。
    _SENSITIVE_ARG_KEYS = (
        "key", "token", "secret", "password", "authorization",
        "credential", "api_key", "access_token", "refresh_token",
    )

    @staticmethod
    def _redact_args(value: Any) -> Any:
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                if any(s in k.lower() for s in TurnEngine._SENSITIVE_ARG_KEYS):
                    out[k] = "***"
                elif isinstance(v, str) and len(v) > 200:
                    out[k] = v[:200] + "…(truncated)"
                else:
                    out[k] = TurnEngine._redact_args(v)
            return out
        if isinstance(value, list):
            return [TurnEngine._redact_args(v) for v in value]
        return value

    def _record_decision(self, kind: str, **fields: Any) -> None:
        """追加一条决策轨迹 entry。

        kind:
          - tool_selection: 模型本轮选择调用的工具 (含可选工具清单 + 实际选择 + 候选)
          - permission: RBAC 权限评估结果
          - scope_escalation: 零信任 scope 越权 → 升级审批
          - approval_resolution: 用户审批结果 (once/always_tool/always_command/deny)
          - plan_decision / directory_decision / question_decision: 交互式工具结果

        每条 entry 含: ts / iteration / kind / agent / session_id (来自 audit_context)
        + 调用方提供的 fields。工具参数类字段(arguments/candidates)递归脱敏后
        再落内存轨迹与 audit 镜像, 避免明文敏感内容进入可回放/可查询的轨迹。
        """
        redacted = {
            k: (self._redact_args(v) if k in ("arguments", "candidates", "entry_args") else v)
            for k, v in fields.items()
        }
        entry: dict[str, Any] = {
            "ts": time.time(),
            "iteration": self._iterations,
            "kind": kind,
            "agent": self.audit_context.get("agent", ""),
            "session_id": self.audit_context.get("session_id", ""),
            **redacted,
        }
        self.decision_trace.append(entry)
        # 镜像到 audit_events 表 (stage=decision_trace) — 离线审计与 engine 解耦。
        if self.audit_sink is not None:
            try:
                self.audit_sink({
                    **self.audit_context,
                    "stage": "decision_trace",
                    "decision_kind": kind,
                    "iteration": self._iterations,
                    "entry": entry,
                })
            except Exception:
                pass  # 审计失败不能阻断主流程

    def get_decision_trace(self) -> list[dict[str, Any]]:
        """返回本 engine 实例累积的完整决策轨迹 (按时间顺序)。"""
        return list(self.decision_trace)

    async def _handle_plan_proposal(self, tool_call: ToolCall) -> AsyncIterator[Event]:
        """Emit the plan for review, await the user's out-of-band decision, and apply it:
        approval flips the live PermissionEngine out of plan mode (the same session keeps
        going, with all its exploration context); rejection keeps plan mode and returns
        the user's feedback so the agent can revise."""
        args = tool_call.arguments or {}
        plan = str(args.get("plan", ""))
        if self.permissions.mode is not Mode.PLAN:
            # The tool is always registered (mode can flip mid-session), but proposing a
            # plan only means something while the session is actually in plan mode. The
            # right next step differs by mode: discuss stays read-only, so the agent
            # should talk through the change; write-capable modes should just do it.
            if self.permissions.mode is Mode.DISCUSS:
                error = (
                    "not in plan mode — this is discuss mode (read-only), so describe "
                    "the proposed changes in chat instead"
                )
            else:
                error = "not in plan mode — proceed with the work directly"
            result: dict[str, Any] = {"approved": False, "error": error}
        elif self.plan_approver is None:
            result = {
                "approved": False,
                "error": "plan approval isn't available here",
            }
        else:
            yield Event(EventType.PLAN_PROPOSED, {"plan": plan})
            self._audit(tool_call, stage="plan_proposed")
            result = await self._interruptible(
                self.plan_approver(dict(args), tool_call.id),
                interrupted={"approved": False, "error": "interrupted by user"},
            ) or {
                "approved": False,
                "error": "no response",
            }

        if result.get("approved"):
            # The approver may pick the post-plan mode ("interactive" asks per write,
            # "auto" executes the approved plan without further prompts).
            try:
                self.permissions.mode = Mode(str(result.get("mode", "interactive")))
            except ValueError:
                self.permissions.mode = Mode.INTERACTIVE
            result = {
                **result,
                "mode": self.permissions.mode.value,
                "note": "plan approved — implement it now",
            }

        status = "ok" if result.get("approved") else "denied"
        self.messages.append(_tool_result_message(tool_call, result))
        self._audit(
            tool_call,
            stage="finished",
            status=status,
            result=result,
            result_preview=_preview(result),
        )
        yield Event(
            EventType.TOOL_FINISHED,
            {
                "name": tool_call.name,
                "status": status,
                "result_preview": _preview(result),
            },
        )

    async def _handle_directory_request(
        self, tool_call: ToolCall
    ) -> AsyncIterator[Event]:
        """Emit the grant prompt, await the user's out-of-band decision (which the requester also
        applies to this session's roots), and return the outcome as the tool result."""
        args = tool_call.arguments or {}
        if self.directory_requester is None:
            result: dict[str, Any] = {
                "granted": False,
                "error": "directory requests aren't available here",
            }
        else:
            yield Event(
                EventType.DIRECTORY_REQUESTED,
                {
                    "reason": str(args.get("reason", "")),
                    "path": str(args.get("path", "")),
                    "writable": bool(args.get("writable", False)),
                },
            )
            self._audit(
                tool_call,
                stage="directory_requested",
                reason=str(args.get("reason", "")),
            )
            result = await self._interruptible(
                self.directory_requester(dict(args), tool_call.id),
                interrupted={"granted": False, "error": "interrupted by user"},
            ) or {
                "granted": False,
                "error": "no response",
            }

        status = "ok" if result.get("granted") else "denied"
        self.messages.append(_tool_result_message(tool_call, result))
        self._audit(
            tool_call,
            stage="finished",
            status=status,
            result=result,
            result_preview=_preview(result),
        )
        yield Event(
            EventType.TOOL_FINISHED,
            {
                "name": tool_call.name,
                "status": status,
                "result_preview": _preview(result),
            },
        )

    async def _handle_ask_user(self, tool_call: ToolCall) -> AsyncIterator[Event]:
        """Emit the question, await the user's out-of-band answer (inline in the live session or
        from the Inbox when unattended), and return it as the tool result."""
        args = tool_call.arguments or {}
        question = str(args.get("question", "")).strip()
        if self.question_asker is None or not question:
            result: dict[str, Any] = {
                "answer": "",
                "error": (
                    "no question was asked"
                    if not question
                    else "asking isn't available here"
                ),
            }
        else:
            # The asker is mode-aware (attended → live inline prompt; unattended → Inbox), so it
            # owns surfacing the question. The engine just awaits the answer.
            self._audit(tool_call, stage="question_requested", reason=question)
            result = await self._interruptible(
                self.question_asker(dict(args), tool_call.id),
                interrupted={"answer": "", "error": "interrupted by user"},
            ) or {
                "answer": "",
                "error": "no response",
            }

        status = "ok" if result.get("answer") else "denied"
        self.messages.append(_tool_result_message(tool_call, result))
        self._audit(
            tool_call,
            stage="finished",
            status=status,
            result=result,
            result_preview=_preview(result),
        )
        yield Event(
            EventType.TOOL_FINISHED,
            {
                "name": tool_call.name,
                "status": status,
                "result_preview": _preview(result),
            },
        )

    def _inject_steering(self) -> None:
        for text, source in self._steering:
            message: dict[str, Any] = {
                "role": "user",
                "content": text,
                "ts": time.time(),
            }
            if source is not None:
                message["source"] = source
            self.messages.append(message)
        self._steering = []

    def _outbound_messages(self) -> list[dict[str, Any]]:
        """`self.messages` prepared for the provider. The SOLE provider feed (see `_astream`).

        Every message is stripped of the display-only sidecars — `source`, `_display`, and
        `ts` — (providers reject unknown keys), unconditionally — whether or not a
        `<system-context>` block is added. When a context
        provider yields a non-empty string, an ephemeral `<system-context>` block is appended to the
        last user message. Never mutates `self.messages`, so neither the strip nor the block is
        persisted/replayed.
        """
        # Strip the display-only sidecars — `source` (connector cards), `_display`
        # (e.g. filter-hidden counts), `ts` (append-time timestamps), and `reasoning`
        # (thinking text) — copying only messages that carry one. Whole `notice` messages
        # (error/interrupted/model-switch markers) are display-only too: dropped entirely.
        _SIDECARS = ("source", "_display", "ts", "reasoning")
        out = [
            (
                {k: v for k, v in msg.items() if k not in _SIDECARS}
                if any(s in msg for s in _SIDECARS)
                else msg
            )
            for msg in self.messages
            if msg.get("role") != "notice"
        ]
        # P0 结构无损裁剪 (GuaAgent 研究文档 #112): 只裁剪 tool 消息的超大输出 /
        # base64 数据 / 元数据块; user / assistant 原文 100% 保留。只在 provider
        # feed 生效 —— self.messages 持久化完整输出, 磁盘/审计链不丢任何内容。
        if self.trim_tool_outputs:
            from .trim import trim_tool_content

            out = [
                (
                    {**msg, "content": trim_tool_content(msg.get("content"))}
                    if msg.get("role") == "tool"
                    else msg
                )
                for msg in out
            ]
        out = _heal_tool_pairing(out)
        # PDF attachments (stored as `file` parts) are adapted to the ACTIVE model right
        # here — never in the persisted history — so a mid-session model switch always
        # re-decides: native PDF models get the real document, the rest get the local
        # text-extract/page-image fallback (pdf_support.py).
        if any(
            isinstance(p, dict) and p.get("type") == "file"
            for msg in out
            if isinstance(msg.get("content"), list)
            for p in msg["content"]
        ):
            caps = self.provider.capabilities(self.model)
            if not getattr(caps, "pdf", False):
                from . import pdf_support

                out = [
                    (
                        {
                            **msg,
                            "content": pdf_support.adapt_content(msg["content"], caps),
                        }
                        if isinstance(msg.get("content"), list)
                        else msg
                    )
                    for msg in out
                ]

        # Images get the same per-turn treatment: a model without vision receives a visible
        # placeholder instead of a payload it would reject. Like the PDF path, this re-decides
        # per call, so a mid-session switch to/from a vision model always does the right thing.
        if any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for msg in out
            if isinstance(msg.get("content"), list)
            for p in msg["content"]
        ):
            caps = self.provider.capabilities(self.model)
            if not getattr(caps, "vision", False):
                placeholder = {
                    "type": "text",
                    "text": "[image attachment — not viewable by this model]",
                }
                out = [
                    (
                        {
                            **msg,
                            "content": [
                                (
                                    placeholder
                                    if isinstance(p, dict)
                                    and p.get("type") == "image_url"
                                    else p
                                )
                                for p in msg["content"]
                            ],
                        }
                        if isinstance(msg.get("content"), list)
                        else msg
                    )
                    for msg in out
                ]

        context = (
            self.context_provider() if self.context_provider is not None else ""
        ) or ""
        if not context:
            return out
        block = f"\n\n<system-context>\n{context}\n</system-context>"
        for i in range(len(out) - 1, -1, -1):
            if out[i].get("role") != "user":
                continue
            msg = dict(out[i])
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = content + block
            elif isinstance(content, list):  # content-parts (text + images)
                msg["content"] = [*content, {"type": "text", "text": block}]
            else:
                msg["content"] = block
            out[i] = msg
            break
        return out


def _assistant_message(turn: AssistantTurn) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": turn.text or "",
        "ts": time.time(),
    }
    if turn.reasoning:
        # Display-only thinking text — rendered by the GUI, stripped for every provider
        # (`_outbound_messages`); provider-private replay blocks go via `extras` instead.
        message["reasoning"] = turn.reasoning
    if turn.extras:
        # Provider-private sidecars (e.g. `_gemini` thought signatures) persist with the
        # message; the owning provider reattaches them, the rest strip them (base.py).
        message.update(turn.extras)
    if turn.tool_calls:
        message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in turn.tool_calls
        ]
    return message


def _tool_result_message(tool_call: ToolCall, result: Any) -> dict[str, Any]:
    content = result if isinstance(result, str) else json.dumps(result, default=str)
    return {
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": content,
        "ts": time.time(),
    }


def _tool_error_message(tool_call: ToolCall, reason: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": json.dumps({"error": "tool call not executed", "reason": reason}),
        "ts": time.time(),
    }


# One model call must not hold the turn forever: a wedged gateway can stream
# nothing indefinitely (the SDK default timeout does not apply to streaming
# iteration). DeepSeek reasoning streams still emit `reasoning` chunks, so 90s
# of absolute silence means the connection is dead, not thinking. Tests shrink
# these via monkeypatch.
_STREAM_STALL_TIMEOUT = 90.0
_STREAM_TOTAL_TIMEOUT = 600.0


def _heal_tool_pairing(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Defensively repair orphaned tool_calls before a provider call.

    OpenAI (and compatible backends) hard-reject a thread where an assistant
    message declares tool_calls that have no matching tool response per
    tool_call_id (HTTP 400 "insufficient tool messages"). Every engine path is
    supposed to answer every call, but a missed edge (e.g. a call suspended
    across a resume boundary) would surface exactly that 400. This pass adds a
    placeholder tool response for any declared call_id that has no response —
    the model then sees "call was dropped" and self-corrects instead of the
    whole turn dying. Read-only: the persisted `self.messages` is untouched.
    """
    healed: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        healed.append(msg)
        tcs = msg.get("tool_calls") if msg.get("role") == "assistant" else None
        if not tcs:
            continue
        declared = {tc.get("id") for tc in tcs if tc.get("id")}
        responded: set[str] = set()
        for nxt in messages[i + 1 :]:
            if nxt.get("role") == "assistant":
                break
            if nxt.get("role") == "tool" and nxt.get("tool_call_id") in declared:
                responded.add(nxt["tool_call_id"])
        for tid in declared - responded:
            healed.append(
                {
                    "role": "tool",
                    "tool_call_id": tid,
                    "content": json.dumps({"error": "tool call was dropped before execution"}),
                }
            )
    return healed


def _preview(value: Any, max_chars: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("\n", "\\n")
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."
