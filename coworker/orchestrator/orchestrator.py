"""QunWork multi-agent orchestration loop (Phase 1 MVP).

Implements the LoopCoop operational loop in its minimal form:
plan (decompose intent into a task DAG) -> dispatch each ready task to an
executor worker -> validate the result with a reviewer worker (validation gate:
ACCEPT / requeue up to max_retries / escalate to human) -> converge.

The governance loop (viscosity/drift health checks, PAUSE/REVERT/ESCALATE
scheduler) and agent mailboxes are Phase 2/3; human escalation surfaces as a
task-level `needs_human` status the caller can route to the existing Inbox.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .governance import ESCALATE, NOP, PAUSE, REVERT, WARN, Governance, GovernanceCommand, GovernanceConfig
from .memory_store import PersistentVectorMemory
from .models import OrchestrationResult, Plan, ReviewVerdict, Task
from .vectormemory import VectorMemory
from .workers import (
    _run_engine_async,
    build_executor_engine,
    build_planner_engine,
    build_reviewer_engine,
)

_MAX_RETRIES_DEFAULT = 2


def _extract_json(text: str) -> Any:
    """Best-effort JSON extraction from a model reply (strip fences/prose)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise ValueError(f"could not parse JSON from worker output: {text[:200]}")


def parse_plan(text: str, *, goal: str) -> Plan:
    """Parse the planner worker's JSON task list into a Plan."""
    data = _extract_json(text)
    if not isinstance(data, list):
        raise ValueError("planner did not return a JSON array of tasks")
    tasks = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not item.get("description"):
            continue
        tid = str(item.get("id") or f"t{i}")
        deps = [str(d) for d in (item.get("deps") or [])]
        tasks.append(Task(id=tid, description=str(item["description"]), deps=deps))
    if not tasks:
        raise ValueError("planner returned an empty task list")
    return Plan(goal=goal, tasks=tasks)


def parse_verdict(text: str) -> ReviewVerdict:
    """Parse the reviewer worker's JSON verdict."""
    data = _extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("reviewer did not return a JSON object")
    return ReviewVerdict(
        accepted=bool(data.get("accepted")),
        reason=str(data.get("reason") or ""),
        confidence=float(data.get("confidence") or 0.0),
        needs_human=bool(data.get("needs_human")),
    )


@dataclass
class Orchestrator:
    """Runs one orchestrated goal to convergence, with an optional governance loop."""

    provider: Any
    model: str
    workspace: str
    model_settings: Optional[dict[str, Any]] = None
    approver: Optional[Any] = None
    max_retries: int = _MAX_RETRIES_DEFAULT
    executor_agent: str = "cowork"
    governance_config: Optional[GovernanceConfig] = None
    embedder: Optional[Any] = None  # optional text->vector callable for drift/memory
    memory: Optional[VectorMemory] = None  # shared blackboard across worker steps
    memory_scope: Optional[str] = None  # persistent-memory scope (e.g. workspace path)
    memory_db: Optional[str] = None  # SQLite path for persistent memory (default workspace/.qunwork/memory.db)
    max_parallel: int = 1  # how many independent tasks run concurrently
    _runs: int = field(default=0, init=False)
    _run_seq: int = field(default=0, init=False)

    # -- planning -----------------------------------------------------------
    async def _plan(self, intent: str) -> Plan:
        engine = build_planner_engine(
            workspace=self.workspace,
            provider=self.provider,
            model=self.model,
            model_settings=self.model_settings,
        )
        text, status = await _run_engine_async(engine, intent)
        if not text:
            raise RuntimeError(f"planner produced no plan (status: {status})")
        return parse_plan(text, goal=intent)

    # -- execution ----------------------------------------------------------
    async def _execute(self, task: Task, *, deps: list[str] = None, hints: list[str] = None) -> str:
        engine = build_executor_engine(
            workspace=self.workspace,
            provider=self.provider,
            model=self.model,
            approver=self.approver,
            agent=self.executor_agent,
            model_settings=self.model_settings,
        )
        parts = [f"Task [{task.id}]: {task.description}\nExecute it now and report the result."]
        if deps:
            parts.append("\nDependencies' results (reuse them):\n" + "\n".join(deps))
        if hints:
            parts.append("\nRelevant prior results (context only):\n" + "\n".join(hints))
        prompt = "\n".join(parts)
        text, status = await _run_engine_async(engine, prompt)
        if not text:
            raise RuntimeError(f"executor produced no result for {task.id} (status: {status})")
        return text

    # -- validation ---------------------------------------------------------
    async def _review(self, task: Task, result: str) -> ReviewVerdict:
        engine = build_reviewer_engine(
            workspace=self.workspace,
            provider=self.provider,
            model=self.model,
            model_settings=self.model_settings,
        )
        prompt = (
            f"Task [{task.id}]: {task.description}\n\n"
            f"Executor's result:\n{result[:4000]}\n\n"
            "Validate the result against the task. Return the JSON verdict."
        )
        text, status = await _run_engine_async(engine, prompt)
        if not text:
            # No verdict → treat as accepted with low confidence rather than looping forever.
            return ReviewVerdict(accepted=True, reason=f"no verdict (status: {status})", confidence=0.3)
        return parse_verdict(text)

    # -- main loop ----------------------------------------------------------
    async def run(self, intent: str) -> OrchestrationResult:
        plan = await self._plan(intent)
        by_id = plan.by_id()
        gov = Governance(goal=intent, config=self.governance_config, embedder=self.embedder)
        self._run_seq += 1
        run_token = str(uuid.uuid4())
        if self.memory is not None:
            mem = self.memory
        elif self.memory_scope is not None:
            db = self.memory_db or str(Path(self.workspace) / ".qunwork" / "memory.db")
            mem = PersistentVectorMemory(db, scope=self.memory_scope, embedder=self.embedder)
        else:
            mem = VectorMemory(embedder=self.embedder)
        gov_log: list[str] = []
        governance_paused = False

        async def process(task: Task) -> bool:
            """Run one task (execute + validate + update). Returns True if progress."""
            self._runs += 1
            task.status = "running"
            deps = [
                f"[{d}] {by_id[d].result}"
                for d in task.deps
                if d in by_id and by_id[d].done and by_id[d].result
            ]
            hints = [
                f"[{h.meta.get('task_id', '?')}] {h.text[:800]}"
                for h in mem.search(task.description, k=2)
                if h.meta.get("task_id") != task.id or h.meta.get("run_token") != run_token
            ]
            try:
                result = await self._execute(task, deps=deps, hints=hints)
            except Exception as exc:  # executor crash → one retry, then escalate
                task.status = "pending" if task.retries < self.max_retries else "needs_human"
                task.result = f"executor error: {exc}"
                task.retries += 1
                return True

            verdict = await self._review(task, result)
            task.confidence = verdict.confidence
            if verdict.accepted:
                task.status = "done"
                task.result = result
            elif verdict.needs_human or task.retries >= self.max_retries:
                task.status = "needs_human"
                task.result = result
                task.retries += 1
            else:
                task.status = "pending"  # requeue for another attempt
                task.result = result
                task.retries += 1
            gov.record_step(task, result, verdict.accepted)
            if task.status == "done":
                mem.add(
                    f"{task.description}\n→ {task.result[:500]}",
                    task_id=task.id,
                    run_token=run_token,
                )
            return True

        # Iterate until convergence: all tasks done, a task escalated to human,
        # the governance loop paused the run, or no progress is possible.
        while not plan.all_done() and not plan.needs_human() and not governance_paused:
            # Governance inspection BEFORE dispatch (every N steps): red lines must
            # block a task before it runs; drift/viscosity checks the live plan.
            if self._runs % gov.config.check_every == 0:
                cmd = gov.inspect(plan)
                gov_log.append(f"[step {self._runs}] {cmd.action}: {cmd.reason} {cmd.metrics}")
                if cmd.action == REVERT:
                    tgt = gov.revert_target(plan)
                    if tgt is not None:
                        gov_log.append(f"[step {self._runs}] REVERT -> re-dispatch {tgt.id}")
                        tgt.status = "pending"
                        tgt.result = ""
                        tgt.confidence = 0.0
                elif cmd.action == WARN:
                    gov.note_warning(cmd)
                elif cmd.action in (PAUSE, ESCALATE):
                    gov.note_warning(cmd)
                    governance_paused = True
                    break

            ready = [t for t in plan.tasks if t.ready(by_id)]
            if not ready:
                blocked = [t.id for t in plan.tasks if t.status == "pending"]
                break
            batch = ready[: max(1, self.max_parallel)]
            results = await asyncio.gather(*(process(t) for t in batch))
            if not any(results):
                break

        status = (
            "paused"
            if governance_paused
            else "needs_human"
            if plan.needs_human()
            else "completed"
            if plan.all_done()
            else "failed"
        )
        summary = "\n\n".join(
            f"[{t.id}] {t.description}\n{t.result}" for t in plan.tasks if t.result
        )
        return OrchestrationResult(
            intent=intent,
            plan=plan,
            summary=summary,
            status=status,
            runs=self._runs,
            governance_report="\n".join(gov_log),
        )
