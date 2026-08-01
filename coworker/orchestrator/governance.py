"""Governance loop (Phase 2) — the LoopCoop governmental loop for QunWork.

Runs asynchronously alongside the operational loop and inspects health metrics
every N steps, emitting a governance command:

    NOP      — normal, keep going
    WARN     — record a warning, keep going
    REVERT   — roll back the latest low-quality task to pending (re-dispatch)
    PAUSE    — halt the operational loop (safety/red-line)
    ESCALATE — escalate to a human (always paired with PAUSE/WARN)

Health metrics (research doc §3.1):
- Loop Viscosity: share of the last k steps that made no progress
  (result text nearly identical to the previous attempt).
- Drift Score: 1 - similarity(goal, current task description). Uses an
  injectable embedder (cosine) when available, else a character-similarity
  fallback (difflib) so the MVP works without a vector store.
- Red-line hits: task descriptions matching configured safety keywords.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Callable, Optional

from .models import Plan, Task

# Governance actions, in increasing severity.
NOP = "NOP"
WARN = "WARN"
REVERT = "REVERT"
PAUSE = "PAUSE"
ESCALATE = "ESCALATE"

# An embedder maps text -> vector; cosine similarity drives drift when provided.
Embedder = Callable[[str], list[float]]


@dataclass
class GovernanceConfig:
    check_every: int = 3  # inspect every N operational steps
    viscosity_window: int = 3  # k in the viscosity definition
    viscosity_epsilon: float = 0.05  # similarity change below this = no progress
    viscosity_high: float = 0.66  # > this share of stuck steps -> REVERT
    viscosity_mid: float = 0.4  # > this -> WARN
    drift_threshold: float = 0.8  # > this -> WARN + ESCALATE (subtasks legitimately differ from the goal)
    red_lines: list[str] = field(default_factory=list)  # e.g. ["drop table", "rm -rf /"]
    max_warnings: int = 3  # repeated WARNs escalate to a human


@dataclass
class StepRecord:
    task_id: str
    description: str
    result: str
    confidence: float
    accepted: bool


@dataclass
class GovernanceCommand:
    action: str  # one of NOP/WARN/REVERT/PAUSE/ESCALATE
    reason: str = ""
    metrics: dict[str, float] = field(default_factory=dict)
    escalate: bool = False  # PAUSE/WARN with human escalation


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def text_similarity(a: str, b: str, embedder: Optional[Embedder] = None) -> float:
    """Similarity in [0, 1]; embedder-based cosine when available, else difflib."""
    if embedder is not None:
        try:
            return max(0.0, _cosine(embedder(a), embedder(b)))
        except Exception:
            pass  # fall through to character similarity
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


class Governance:
    """Tracks operational-loop health and emits governance commands."""

    def __init__(
        self,
        *,
        goal: str,
        config: Optional[GovernanceConfig] = None,
        embedder: Optional[Embedder] = None,
    ) -> None:
        self.goal = goal
        self.config = config or GovernanceConfig()
        self.embedder = embedder
        self.steps: list[StepRecord] = []
        self.warnings: list[str] = []
        self._reverted: set[str] = set()
        self._checked = 0

    # -- recording ----------------------------------------------------------
    def record_step(self, task: Task, result: str, accepted: bool) -> None:
        self.steps.append(
            StepRecord(
                task_id=task.id,
                description=task.description,
                result=result,
                confidence=task.confidence,
                accepted=accepted,
            )
        )

    # -- metrics ------------------------------------------------------------
    def viscosity(self) -> float:
        """Share of the last k steps that made no progress (near-duplicate results)."""
        recent = self.steps[-self.config.viscosity_window :]
        if len(recent) < 2:
            return 0.0
        stuck = 0
        for prev, cur in zip(recent, recent[1:]):
            sim = text_similarity(prev.result, cur.result, self.embedder)
            if sim >= 1.0 - self.config.viscosity_epsilon:
                stuck += 1
        return stuck / (len(recent) - 1)

    def drift(self, plan: Plan) -> float:
        """1 - similarity(goal, the task currently being worked)."""
        current = next((t for t in plan.tasks if t.status == "running"), None)
        text = current.description if current else (plan.tasks[-1].description if plan.tasks else self.goal)
        return 1.0 - text_similarity(self.goal, text, self.embedder)

    def red_line_hit(self, plan: Plan) -> bool:
        if not self.config.red_lines:
            return False
        haystack = " ".join(t.description.lower() for t in plan.tasks if not t.done)
        return any(kw.lower() in haystack for kw in self.config.red_lines)

    def autonomy_ratio(self) -> float:
        """Share of steps accepted without rework."""
        if not self.steps:
            return 1.0
        return sum(1 for s in self.steps if s.accepted) / len(self.steps)

    # -- decision -----------------------------------------------------------
    def inspect(self, plan: Plan) -> GovernanceCommand:
        """Decision tree mirroring the research doc's governmental-loop scheduler."""
        self._checked += 1
        metrics = {
            "viscosity": round(self.viscosity(), 3),
            "drift": round(self.drift(plan), 3),
            "autonomy": round(self.autonomy_ratio(), 3),
            "steps": float(len(self.steps)),
        }

        if self.red_line_hit(plan):
            return GovernanceCommand(
                PAUSE, reason="red-line keyword matched in pending tasks", metrics=metrics, escalate=True
            )
        if len(self.warnings) >= self.config.max_warnings:
            return GovernanceCommand(
                ESCALATE, reason="too many repeated warnings", metrics=metrics, escalate=True
            )
        if metrics["drift"] > self.config.drift_threshold:
            return GovernanceCommand(
                WARN, reason=f"goal drift {metrics['drift']} exceeds threshold", metrics=metrics, escalate=True
            )
        if metrics["viscosity"] > self.config.viscosity_high:
            return GovernanceCommand(
                REVERT, reason=f"loop viscosity {metrics['viscosity']} too high", metrics=metrics
            )
        if metrics["viscosity"] > self.config.viscosity_mid:
            return GovernanceCommand(
                WARN, reason=f"loop viscosity {metrics['viscosity']} elevated", metrics=metrics
            )
        return GovernanceCommand(NOP, reason="nominal", metrics=metrics)

    def note_warning(self, cmd: GovernanceCommand) -> None:
        self.warnings.append(cmd.reason)

    def revert_target(self, plan: Plan) -> Optional[Task]:
        """Pick the lowest-confidence completed task (not yet reverted) to re-dispatch."""
        done = [t for t in plan.tasks if t.done and t.id not in self._reverted]
        if not done:
            return None
        tgt = min(done, key=lambda t: t.confidence)
        if tgt.confidence < 0.5:
            self._reverted.add(tgt.id)
            return tgt
        return None
