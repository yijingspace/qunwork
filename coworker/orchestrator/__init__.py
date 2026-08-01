"""QunWork multi-agent loop-cooperation (Phase 1 MVP).

Provides the Orchestrator (plan -> dispatch -> validate -> converge) and an
`orchestrate` tool that can be mounted into an engine's toolset (like `explore`),
so the main agent can delegate a whole goal to a swarm of worker agents.
"""

from __future__ import annotations

from typing import Any, Optional

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
    "run_orchestration",
    "orchestration_tools",
]


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
) -> list:
    """Tool-set entry: exposes `orchestrate` to a parent engine (like `explore`)."""

    def orchestrate(intent: str) -> dict:
        """Delegate an entire multi-step goal to a swarm of worker agents: a planner
        decomposes it into a task plan, executor agents do the work, and a reviewer
        validates each result (rework up to the retry limit, escalate to you if a
        task needs a human decision). Returns the converged task report.

        Args:
            intent (str): The goal, with constraints and the expected deliverable.
        """
        result = run_orchestration(
            intent=intent,
            workspace=workspace,
            provider=provider,
            model=model,
            model_settings=model_settings,
            approver=approver,
        )
        out: dict[str, Any] = {"status": result.status, "report": result.task_report()}
        if result.status != "completed":
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
