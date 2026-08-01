"""Orchestration data model for QunWork multi-agent loop cooperation (Phase 1).

Mirrors the LoopCoop task-queue Q / structured-state S_t concepts in the research
docs, kept minimal for the MVP: a task DAG (list + deps), per-task status/result,
and a run-level summary. No vector memory / governance loop yet (Phase 2+).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Task:
    """A single unit of work in the orchestrated plan (a node in the task DAG)."""

    id: str
    description: str
    deps: list[str] = field(default_factory=list)
    # pending | running | done | needs_human
    status: str = "pending"
    result: str = ""
    retries: int = 0
    confidence: float = 0.0

    @property
    def done(self) -> bool:
        return self.status == "done"

    def ready(self, by_id: dict[str, "Task"]) -> bool:
        """A task is ready when pending and every dependency is done."""
        if self.status != "pending":
            return False
        return all(by_id.get(d) is not None and by_id[d].done for d in self.deps)


@dataclass
class Plan:
    """The parsed task plan produced by the planner worker."""

    goal: str
    tasks: list[Task] = field(default_factory=list)

    def by_id(self) -> dict[str, Task]:
        return {t.id: t for t in self.tasks}

    def all_done(self) -> bool:
        return all(t.done for t in self.tasks)

    def needs_human(self) -> bool:
        return any(t.status == "needs_human" for t in self.tasks)


@dataclass
class ReviewVerdict:
    """Structured verdict from the reviewer worker (validation gate)."""

    accepted: bool
    reason: str = ""
    confidence: float = 0.0
    needs_human: bool = False


@dataclass
class OrchestrationResult:
    """Final outcome of an orchestrated run."""

    intent: str
    plan: Plan
    summary: str = ""
    status: str = "completed"  # completed | needs_human | paused | failed
    runs: int = 0
    governance_report: str = ""  # health metrics + governance commands, if any
    report_path: str = ""  # file the assembled report was written to (if any)

    def final_report(self) -> str:
        """The finished deliverable: the consolidation task's full output if it
        exists, else the assembled task summary."""
        done = [t for t in self.plan.tasks if t.done and t.result]
        if done:
            return done[-1].result
        return self.summary

    def task_report(self) -> str:
        lines = [f"Goal: {self.plan.goal}"]
        for t in self.plan.tasks:
            mark = "✓" if t.done else ("⚠" if t.status == "needs_human" else "✗")
            conf = f" (confidence {t.confidence:.2f})" if t.confidence else ""
            lines.append(f"{mark} [{t.id}] {t.description}{conf}")
            if t.result:
                lines.append(f"    {t.result[:400]}")
        return "\n".join(lines)
