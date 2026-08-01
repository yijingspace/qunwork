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
from typing import Any, Optional

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
_EXECUTOR_MAX_ITERATIONS = 20
_REVIEWER_MAX_ITERATIONS = 8

PLANNER_INSTRUCTIONS = """You are the planning agent of a multi-agent swarm. \
Break the user's goal into a small, ordered task plan. Return ONLY a JSON array, no \
prose, no markdown fences. Each element: {"id": "t1", "description": "...", "deps": ["t0"]}. \
Use ids t0, t1, ...; deps must reference earlier ids (empty list for the first tasks). \
Keep the plan to 3-6 concrete tasks; each task must be independently executable and \
produce a tangible result."""

REVIEWER_INSTRUCTIONS = """You are the review agent of a multi-agent swarm. You are \
given a task and the executor's result. Validate whether the result actually satisfies \
the task. Return ONLY a JSON object, no prose, no markdown fences: \
{"accepted": true|false, "confidence": 0.0-1.0, "reason": "one sentence", \
"needs_human": false}. Set accepted=true when the result is good enough; accepted=false \
with needs_human=true when the task cannot be completed without a human decision \
(unclear requirements, missing permissions, safety boundary)."""

EXECUTOR_INSTRUCTIONS = """You are an execution agent in a multi-agent swarm. \
Complete the single task you are given, end to end, using the available tools \
(files, search, shell, todo). Do the actual work and produce the deliverable; do not \
just describe what you would do. Your final message is the task result report."""


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


async def _run_engine_async(engine: TurnEngine, prompt: str) -> tuple[str, str]:
    """Run a child engine and collect its final text + status (async context)."""
    report, status = "", "unknown"
    async for event in engine.run(prompt):
        if event.type == EventType.ASSISTANT_MESSAGE and event.data.get("text"):
            report = event.data["text"]
        elif event.type == EventType.TURN_END:
            status = event.data.get("status", "unknown")
        elif event.type == EventType.ERROR:
            return report, f"error: {event.data.get('error', '')}"
    return report, status


def _run_engine_collect(engine: TurnEngine, prompt: str) -> tuple[str, str]:
    """Synchronous wrapper for worker-thread / tool contexts (no running loop)."""

    return asyncio.run(_run_engine_async(engine, prompt))
