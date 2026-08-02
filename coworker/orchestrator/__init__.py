"""QunWork multi-agent loop-cooperation (Phase 1 MVP).

Provides the Orchestrator (plan -> dispatch -> validate -> converge) and an
`orchestrate` tool that can be mounted into an engine's toolset (like `explore`),
so the main agent can delegate a whole goal to a swarm of worker agents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import aisuite as ai

from .models import OrchestrationResult, Plan, ReviewVerdict, Task
from .orchestrator import Orchestrator
from .vectormemory import VectorMemory
from .memory_store import PersistentVectorMemory

__all__ = [
    "Orchestrator",
    "OrchestrationResult",
    "Plan",
    "ReviewVerdict",
    "Task",
    "VectorMemory",
    "PersistentVectorMemory",
    "auto_approver",
    "run_orchestration",
    "orchestration_tools",
]


def auto_approver():
    """An Approver that auto-allows worker writes for the duration of the run.

    Running a swarm is itself the user's explicit authorization to work in the
    chosen workspace; per-tool approval prompts would deadlock the headless
    workers (nothing is there to click). Safety still holds: the governance
    loop watches red-lines/drift, and workers stay inside the workspace.
    """

    from ..engine import ApprovalOutcome

    async def approve(request):
        return ApprovalOutcome.ALWAYS_TOOL

    return approve


def run_orchestration(
    *,
    intent: str,
    workspace: str,
    provider: Any,
    model: str,
    model_settings: Optional[dict[str, Any]] = None,
    approver: Optional[Any] = None,
    max_retries: int = 2,
    executor_agent: str = "cowork",
    memory_scope: Optional[str] = None,
    event_sink: Optional[Callable[[str, dict], None]] = None,
    timeout_seconds: Optional[int] = None,
    max_parallel: int = 1,
) -> OrchestrationResult:
    """Run one orchestrated goal synchronously (worker-thread context)."""
    orch = Orchestrator(
        provider=provider,
        model=model,
        workspace=workspace,
        model_settings=model_settings,
        approver=approver,
        max_retries=max_retries,
        executor_agent=executor_agent,
        memory_scope=memory_scope,
        event_sink=event_sink,
        timeout_seconds=timeout_seconds,
        max_parallel=max_parallel,
    )
    import asyncio

    return asyncio.run(orch.run(intent))


def orchestration_tools(
    *,
    workspace: str,
    provider: Any,
    model: str,
    model_settings: Optional[dict[str, Any]] = None,
    approver: Optional[Any] = None,
    executor_agent: str = "cowork",
) -> list:
    """Tool-set entry: exposes `orchestrate` to a parent engine (like `explore`)."""

    def orchestrate(intent: str) -> dict:
        """Delegate an entire multi-step goal to a swarm of worker agents: a planner
        decomposes it into a task plan, executor agents do the work, and a reviewer
        validates each result (rework up to the retry limit, escalate to you if a
        task needs a human decision). The converged deliverable is WRITTEN to a file
        and returned in full.

        Args:
            intent (str): The goal, with constraints and the expected deliverable.
                Name the target output file inside the intent (e.g. 写入 report.md)
                when you want the deliverable saved under a specific name.
        """
        # Per-workspace run store so main-session runs are traceable & comparable
        # with panel runs (duration, tasks, governance) — same SQLite schema.
        from .run_store import OrchestrationRunStore

        store = OrchestrationRunStore(Path(workspace) / ".qunwork" / "orchestration.db")
        run_id = store.create_run(intent)
        result = run_orchestration(
            intent=intent,
            workspace=workspace,
            provider=provider,
            model=model,
            # Do NOT inherit the parent session's model_settings: output caps there
            # (e.g. a small max_tokens) truncate worker JSON plans and stall the
            # swarm. Workers use the provider's defaults.
            model_settings=None,
            # share episodic memory across runs for this workspace so later swarm
            # runs (and the main session) benefit from earlier lessons
            memory_scope=str(workspace),
            approver=approver,
            event_sink=lambda kind, payload: store.append_event(run_id, kind, payload),
            executor_agent=executor_agent,
        )
        store.update_status(run_id, result.status, final=result.final_report())
        out: dict[str, Any] = {
            "status": result.status,
            # the finished deliverable (consolidation output) + where it was saved
            "report": result.final_report(),
            "report_path": result.report_path,
        }
        if result.report_path:
            out["note"] = (
                "The complete deliverable has ALREADY been written to "
                f"{result.report_path}. Do NOT write or overwrite that file again — "
                "present the report to the user as-is."
            )
        elif result.status != "completed":
            out["note"] = (
                "not fully completed — see the task report for what needs human attention"
            )
        return out

    return [
        ai.tool(
            orchestrate,
            metadata=ai.ToolMetadata(
                category="orchestration",
                risk_level="medium",
                capabilities=["orchestration"],
                requires_approval=False,
            ),
        )
    ]
