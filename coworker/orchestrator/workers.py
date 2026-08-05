"""Worker engines for QunWork multi-agent orchestration (Phase 1).

Three worker roles mirror the LoopCoop operational loop:
- Planner  (规划): decomposes the top-level intent into a task DAG (JSON).
- Executor (执行): runs one task end-to-end with the full toolset + approval gate.
- Reviewer (评审): validates a task's result (ACCEPT / needs rework / needs human).

Planner & Reviewer are read-only (plan mode, like the `explore` subagent);
Executor is built from the full `build_engine` with a caller-supplied approver so
every write/shell action still goes through the existing permission gate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Optional

import aisuite as ai

from ..agents import get_agent
from ..engine import TurnEngine
from ..events import EventType
from ..permissions import Mode, PermissionEngine
from ..tools import ToolRegistry
from ..tools.files import file_tools
from ..tools.git import git_tools
from ..tools.search import search_tools

_PLANNER_MAX_ITERATIONS = 8
_EXECUTOR_MAX_ITERATIONS = 24
_REVIEWER_MAX_ITERATIONS = 8

PLANNER_INSTRUCTIONS = """You are the planning agent of a multi-agent swarm. \
Break the user's goal into a small, ordered task plan. Return ONLY a JSON array, no \
prose, no markdown fences. Each element: {"id": "t1", "description": "...", "deps": ["t0"]}. \
Use ids t0, t1, ...; deps must reference earlier ids (empty list for the first tasks).

PARALLELISM IS KEY: make tasks as INDEPENDENT as possible so many run at once. \
Chapter/part tasks should have EMPTY deps (each writes its own section from \
knowledge, verifying key figures if convenient). Only the final consolidation task \
depends on all earlier ones. Aim for 4-8 tasks: several independent writing tasks + \
one final assemble/consolidate task with deps on all others. Keep each task \
independently executable and tangible ("write/analyze/draft <deliverable>")."""

REVIEWER_INSTRUCTIONS = """You are the review agent of a multi-agent swarm. You are \
given a task and the executor's result. Validate whether the result actually satisfies \
the task. Return ONLY a JSON object, no prose, no markdown fences: \
{"accepted": true|false, "confidence": 0.0-1.0, "reason": "one sentence", \
"needs_human": false}. Set accepted=true when the result is good enough; accepted=false \
with needs_human=true when the task cannot be completed without a human decision \
(unclear requirements, missing permissions, safety boundary)."""

EXECUTOR_INSTRUCTIONS = """You are an execution agent in a multi-agent swarm. \
Complete the single task you are given, end to end, using your knowledge and the \
available tools (files, search, shell, web).

- WRITE FIRST, VERIFY LATER: start producing the deliverable immediately from your \
knowledge. Only after the draft is written may you verify a few key figures online.
- DELIVER AND STOP: once you have produced the deliverable and (when asked) written it \
to a file, STOP immediately and report the result. Do not keep re-reading files, \
re-searching, or polishing — extra turns just burn the task budget and the timeout \
drops your finished work.
- YOUR FINAL MESSAGE IS THE DELIVERABLE ITSELF: the finished content, not a report \
about it. Never end with process sentences like \"I will now write…\", \"now stitching \
the fragments…\", \"verifying…\" — if you still need to write/stitch, DO it inside this \
same turn and end with the product text.
- AFTER WRITING A FILE: your final message must CONTAIN THE FULL FILE CONTENT (read \
the file back if needed). The orchestrator stitches workers' final messages into the \
deliverable — content that lives only on disk is lost. \"Written to x.md\" alone is \
never an acceptable final message.
- CHINESE TEXT CHECKS: use the text_stats tool (or Python via the shell with \
UTF-8 output) to count characters. NEVER write PowerShell inline scripts that \
contain Chinese literals — ANSI mojibake has repeatedly stalled tasks. If a \
verification keeps failing on encoding, SKIP it and deliver the content anyway.
- Web/search are never a substitute for writing: cap them at 2 calls per task, each \
at most once per query. If a call fails or is slow, proceed — mark uncertain figures \
with \"~\" plus a note.
- When a TOOL is missing or misbehaving (grep errors, quoting problems, a recurring \
workaround, a helper you wish existed): create a reusable skill with the create_skill \
tool (name/description/body), then load_skill it. Skills persist to the catalog for \
future runs — building your own toolbelt is part of the job.
- Do not narrate plans (\"I will now fetch…\"). Just do the work.
- Keep the deliverable self-contained (it becomes part of the final report). \
Your final message is the task result report: the deliverable itself, with data caveats."""



def _readonly_engine(
    *,
    workspace: str | Path,
    provider: Any,
    model: str,
    instructions: str,
    max_iterations: int,
    model_settings: Optional[dict[str, Any]] = None,
) -> TurnEngine:
    """A child engine with read-only tools (plan mode) and a fresh context."""
    ws = str(Path(workspace).resolve())
    registry = ToolRegistry()
    replaced = {"search_files", "read_file", "read_file_lines"}
    registry.register_all(
        [
            t
            for t in ai.toolkits.files(root=ws)
            if getattr(t, "__name__", "") not in replaced
        ]
    )
    registry.register_all(file_tools(ws))
    registry.register_all(ai.toolkits.git(root=ws))
    registry.register_all(git_tools(ws))
    registry.register_all(search_tools(ws))
    permissions = PermissionEngine(workspace_root=Path(ws), mode=Mode.PLAN)
    return TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model=model,
        instructions=instructions,
        max_iterations=max_iterations,
        model_settings=model_settings,
    )


def build_planner_engine(
    *,
    workspace: str | Path,
    provider: Any,
    model: str,
    model_settings: Optional[dict[str, Any]] = None,
) -> TurnEngine:
    return _readonly_engine(
        workspace=workspace,
        provider=provider,
        model=model,
        instructions=PLANNER_INSTRUCTIONS,
        max_iterations=_PLANNER_MAX_ITERATIONS,
        model_settings=model_settings,
    )


def build_reviewer_engine(
    *,
    workspace: str | Path,
    provider: Any,
    model: str,
    model_settings: Optional[dict[str, Any]] = None,
) -> TurnEngine:
    return _readonly_engine(
        workspace=workspace,
        provider=provider,
        model=model,
        instructions=REVIEWER_INSTRUCTIONS,
        max_iterations=_REVIEWER_MAX_ITERATIONS,
        model_settings=model_settings,
    )


def build_executor_engine(
    *,
    workspace: str | Path,
    provider: Any,
    model: str,
    approver: Optional[Any] = None,
    agent: str = "cowork",
    model_settings: Optional[dict[str, Any]] = None,
) -> TurnEngine:
    """Executor with the full toolset + the caller's approval gate."""
    from ..agent import build_engine  # lazy: avoids circular import (agent ↔ orchestrator)

    ws = str(Path(workspace).resolve())
    Path(ws).mkdir(parents=True, exist_ok=True)
    engine = build_engine(
        agent=get_agent(agent),
        workspace=ws,
        model=model,
        mode=Mode.INTERACTIVE,
        approver=approver,
        provider=provider,
        max_iterations=_EXECUTOR_MAX_ITERATIONS,
        model_settings=model_settings,
    )
    # Reinforce the single-task execution contract on top of the persona prompt.
    engine.messages.insert(0, {"role": "system", "content": EXECUTOR_INSTRUCTIONS})
    return engine


async def _run_engine_async(
    engine: TurnEngine,
    prompt: str,
    on_event: Optional[Callable[[str, dict], None]] = None,
) -> tuple[str, str]:
    """Run a child engine and collect its final text + status (async context).
    `on_event` receives (kind, payload) for intermediate assistant text so the
    caller can stream the worker's chain-of-thought (thought feed)."""
    report, status = "", "unknown"
    async for event in engine.run(prompt):
        if event.type == EventType.ASSISTANT_MESSAGE and event.data.get("text"):
            text = event.data["text"]
            report = text
            if on_event:
                on_event("worker_thought", {"text": text})
        elif event.type in (EventType.TOOL_STARTED, EventType.TOOL_FINISHED):
            # Tool heartbeat: worker tool rounds emit no assistant text, so the
            # run's event stream would freeze during long tool chains (e.g. the
            # consolidation task reading drafts + running verify scripts) — the
            # deck then misjudges the run as stale. Surface tool progress so the
            # stream keeps ticking.
            if on_event:
                name = event.data.get("name") or "tool"
                if event.type == EventType.TOOL_STARTED:
                    on_event("worker_thought", {"text": f"⚙ {name}…"})
                else:
                    status = event.data.get("status") or ""
                    on_event("worker_thought", {"text": f"✓ {name} {status}".strip()})
        elif event.type == EventType.TURN_END:
            status = event.data.get("status", "unknown")
        elif event.type == EventType.ERROR:
            return report, f"error: {event.data.get('error', '')}"
    return report, status


def _run_engine_collect(engine: TurnEngine, prompt: str) -> tuple[str, str]:
    """Synchronous wrapper for worker-thread / tool contexts (no running loop)."""

    return asyncio.run(_run_engine_async(engine, prompt))
