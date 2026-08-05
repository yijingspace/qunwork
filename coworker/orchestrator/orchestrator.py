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
import time
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

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


def task_phase(task: Any, plan: Any) -> int:
    """T5 periodic memory slot for a task: its ordinal in the plan, mod the
    Pisano period 60 (π(10)). Tasks at the same phase (e.g. the 5th task of
    every ~60-task run) share same-phase history in the memory pool — the
    DPNN closed-loop idea applied to swarm memory reuse."""
    try:
        idx = plan.tasks.index(task)
    except (ValueError, AttributeError):
        return 0
    return idx % 60


def _looks_like_interim(text: Optional[str]) -> bool:
    """Detect an executor reply that is a PROCESS NOTE rather than the deliverable:
    too short to be a chapter, or an explicit action-phrase lead-in. When true, the
    orchestrator pushes one more turn demanding the full product (and the reviewer
    still guards quality afterwards)."""
    t = (text or "").strip()
    if not t:
        return True
    if len(t) < 120:
        return True
    heads = (
        "i will", "let me", "now i", "i'm going", "i am going",
        "我将", "让我", "我先", "正在", "接下来", "现在", "运行核验", "查看", "查找", "检查", "收集",
    )
    low = t.lower()
    return any(low.startswith(h) for h in heads)


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
        # truncated JSON: try to close a dangling array/object before giving up
        stripped = text.strip()
        if stripped:
            for closer, closer_rev in (("]", "["), ("}", "{")):
                if stripped.count(closer_rev) > stripped.count(closer):
                    try:
                        return json.loads(stripped + closer)
                    except json.JSONDecodeError:
                        pass
    raise ValueError(f"could not parse JSON from worker output: {text[:200]}")


def parse_plan(text: str, *, goal: str) -> Plan:
    """Parse the planner worker's JSON task list into a Plan, with multi-level
    fallbacks: strict JSON array → extracted "description" fields → line items."""
    data = None
    try:
        data = _extract_json(text)
    except ValueError:
        pass
    if isinstance(data, list):
        tasks = []
        for i, item in enumerate(data):
            if not isinstance(item, dict) or not item.get("description"):
                continue
            tid = str(item.get("id") or f"t{i}")
            deps = [str(d) for d in (item.get("deps") or [])]
            tasks.append(Task(id=tid, description=str(item["description"]), deps=deps))
        if tasks:
            return Plan(goal=goal, tasks=tasks)
    # fallback 1: extract every "description": "…" field
    descs = re.findall(r'"description"\s*:\s*"([^"]+)"', text)
    if descs:
        return Plan(
            goal=goal,
            tasks=[Task(id=f"t{i}", description=d) for i, d in enumerate(descs)],
        )
    # fallback 2: treat bullet/numbered lines as tasks
    lines = [
        re.sub(r"^[-*\d.\s\u2022]+", "", ln).strip()
        for ln in text.splitlines()
        if re.match(r"^\s*[-*\d.\u2022]", ln) and len(ln.strip()) > 10
    ]
    if lines:
        return Plan(
            goal=goal,
            tasks=[Task(id=f"t{i}", description=d) for i, d in enumerate(lines)],
        )
    raise ValueError(f"could not parse task plan from worker output: {text[:300]}")


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


def clean_thought(text: str, worker: str = "") -> str:
    """Normalize a worker's raw chain-of-thought line for display: verdict/plan
    JSON becomes a readable sentence; artifact links, code fences, delivery
    shells and debugging noise are stripped; long lines are truncated."""
    t = text.strip()
    if not t:
        return ""
    if worker == "reviewer" and t.startswith("{"):
        try:
            d = json.loads(t)
            mark = "✓ 通过" if d.get("accepted") else ("⚠ 需人工" if d.get("needs_human") else "↻ 重做")
            conf = d.get("confidence")
            reason = str(d.get("reason") or "").strip()
            tail = f": {reason}" if reason else ""
            return f"{mark}（置信度 {conf}）{tail}"[:300]
        except Exception:
            pass
    if worker == "planner" and t.startswith("["):
        try:
            tasks = json.loads(t)
            names = []
            for i, x in enumerate(tasks):
                if isinstance(x, dict) and x.get("description"):
                    names.append(f"t{i} {str(x['description'])[:24]}")
            if names:
                return f"规划 {len(names)} 个任务: " + "; ".join(names)[:300]
        except Exception:
            pass
    # artifact/file links -> bare text
    t = re.sub(r"\[([^\]]*)\]\((?:artifact|file|attachment):[^)]*\)", r"\1", t)
    # code fences -> placeholder
    t = re.sub(r"```[a-zA-Z]*\n.*?```", "［代码已省略］", t, flags=re.DOTALL)
    # delivery-shell header line ("**Task [t0] 交付…**")
    t = re.sub(r"^\**\s*Task\s*\[[^\]]*\]\s*[^*\n]*\**\s*\n", "", t)
    return t[:400]


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
    max_parallel: int = 4  # how many independent tasks run concurrently
    timeout_seconds: Optional[int] = 600  # whole-run timeout (None = no limit)
    task_timeout_seconds: Optional[int] = 240  # per-task timeout; timeout degrades to a partial result
    event_sink: Optional[Callable[[str, dict], None]] = None  # (kind, payload) progress feed
    # G2 command deck: external control (pause/resume/operator message/requeue
    # approval). None = headless run with auto-requeue (legacy behaviour).
    controller: Optional[Any] = None
    requeue_approval_timeout: float = 120.0
    # T4 convergence guard (LoopCoop fixed-point): how many consecutive rounds
    # without real progress (no new done task, no changed result, no accepted
    # requeue) before the run is declared stalled instead of spinning forever.
    stall_rounds_threshold: int = 2
    _runs: int = field(default=0, init=False)
    _run_seq: int = field(default=0, init=False)
    _last_plan: Optional[Plan] = field(default=None, init=False)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.event_sink is not None:
            try:
                self.event_sink(kind, payload)
            except Exception:
                pass

    def _worker_feed(self, worker: str, task_id: str = "") -> Callable[[str, dict], None]:
        """Wrap a worker engine's on_event into sink events (chain-of-thought feed),
        normalized for display: raw JSON, code, artifact links and delivery shells
        are distilled into readable lines."""

        def feed(kind: str, payload: dict[str, Any]) -> None:
            if kind == "worker_thought":
                self._emit(
                    "worker_thought",
                    {
                        "worker": worker,
                        "task_id": task_id,
                        "text": clean_thought(str(payload.get("text", "")), worker),
                    },
                )

        return feed

    # -- planning -----------------------------------------------------------
    async def _plan(self, intent: str) -> Plan:
        last_err: Exception | None = None
        for attempt in range(3):  # planner JSON can be flaky — retry before giving up
            try:
                engine = build_planner_engine(
                    workspace=self.workspace,
                    provider=self.provider,
                    model=self.model,
                    model_settings=self.model_settings,
                )
                text, status = await _run_engine_async(engine, intent, on_event=self._worker_feed("planner"))
                if not text:
                    last_err = RuntimeError(f"planner produced no plan (status: {status})")
                    continue
                return parse_plan(text, goal=intent)
            except (ValueError, RuntimeError) as exc:
                last_err = exc
                self._emit("planner_retry", {"attempt": attempt + 1, "error": str(exc)})
        raise RuntimeError(f"planner failed after 3 attempts: {last_err}")

    # -- execution ----------------------------------------------------------
    async def _execute(
        self,
        task: Task,
        *,
        deps: list[str] = None,
        hints: list[str] = None,
        on_text: Optional[Callable[[str], None]] = None,
    ) -> str:
        from . import auto_approver

        engine = build_executor_engine(
            workspace=self.workspace,
            provider=self.provider,
            model=self.model,
            # Auto-approve worker writes by default (running the swarm IS the
            # authorization); callers may override with their own approver.
            approver=self.approver if self.approver is not None else auto_approver(),
            agent=self.executor_agent,
            model_settings=self.model_settings,
        )
        parts = [f"Task [{task.id}]: {task.description}\nExecute it now and report the result."]
        if deps:
            parts.append("\nDependencies' results (reuse them):\n" + "\n".join(deps))
        if hints:
            parts.append("\nRelevant prior results (context only):\n" + "\n".join(hints))
        prompt = "\n".join(parts)

        def feed(kind: str, payload: dict[str, Any]) -> None:
            if kind == "worker_thought" and payload.get("text"):
                raw = str(payload["text"])
                if on_text is not None:
                    on_text(raw)  # keep raw for the timeout-degradation draft
                self._emit(
                    "worker_thought",
                    {"worker": "executor", "task_id": task.id, "text": clean_thought(raw, "executor")},
                )

        text, status = await _run_engine_async(engine, prompt, on_event=feed)
        if _looks_like_interim(text):
            # Deliverable push: the model stopped with a process note ("I will
            # verify…", "Let me check…") instead of the product — observed on
            # every weekly-report task. The engine keeps its message history, so
            # one more grounded turn demanding the full deliverable fixes most
            # cases; the reviewer still guards the rest.
            text2, status2 = await _run_engine_async(
                engine,
                "Your previous reply was only a process note, not the deliverable. "
                "Output the COMPLETE deliverable now — the full chapter text — as "
                "your final message. If you already wrote a file, read it back and "
                "paste its full content. Do NOT describe actions; paste the product.",
                on_event=feed,
            )
            if text2 and not _looks_like_interim(text2):
                text = text2
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
        text, status = await _run_engine_async(
            engine, prompt, on_event=self._worker_feed("reviewer", task.id)
        )
        if not text:
            # No verdict → treat as accepted with low confidence rather than looping forever.
            return ReviewVerdict(accepted=True, reason=f"no verdict (status: {status})", confidence=0.3)
        return parse_verdict(text)

    # -- main loop ----------------------------------------------------------
    async def run(self, intent: str) -> OrchestrationResult:
        # Soft budget: the deadline lives inside the scheduling loop, so a timeout
        # stops NEW batches instead of truncating tasks that are ready or in
        # flight (previously a 300s budget could kill the consolidation task t4
        # right as its dependencies finished).
        deadline = None
        if self.timeout_seconds:
            deadline = time.monotonic() + self.timeout_seconds
        result = await self._run(intent, deadline=deadline)
        # A timed-out run that still assembled a real deliverable counts as
        # completed — the timeout note alone is not a deliverable.
        if result.status == "paused" and result.report_path:
            report = result.final_report()
            if report.strip() and not report.startswith("swarm timed out"):
                result.status = "completed"
        return result

    async def _run(self, intent: str, deadline: Optional[float] = None) -> OrchestrationResult:
        self._emit("run_started", {"intent": intent})
        # G2: operator directives accumulate per scheduling round (see while-loop).
        directives: list[str] = []
        # T4: consecutive no-progress rounds → stall (fixed point without completion).
        stall_rounds = 0
        stalled_reason: Optional[str] = None
        plan = await self._plan(intent)
        self._last_plan = plan
        self._emit(
            "plan_ready",
            {
                "goal": intent,
                "tasks": [
                    {"id": t.id, "description": t.description, "deps": t.deps}
                    for t in plan.tasks
                ],
            },
        )
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
        budget_exhausted = False  # set when the soft deadline passes; stops new batches

        async def process(task: Task) -> bool:
            """Run one task (execute + validate + update). Returns True if progress."""
            self._runs += 1
            task.status = "running"
            self._emit("task_started", {"id": task.id, "description": task.description, "attempt": task.retries + 1})
            deps = [
                f"[{d}] {by_id[d].result}"
                for d in task.deps
                if d in by_id and by_id[d].done and by_id[d].result
            ]
            hints = [
                f"[{h.meta.get('task_id', '?')}] {h.text[:800]}"
                for h in mem.search(
                    task.description,
                    k=2,
                    phase=task_phase(task, plan),
                )
                if h.meta.get("task_id") != task.id or h.meta.get("run_token") != run_token
            ]
            collected: list[str] = []
            if directives:
                hints = hints + [f"[operator] {d}" for d in directives]

            async def _run_task() -> str:
                return await self._execute(
                    task, deps=deps, hints=hints, on_text=collected.append
                )

            try:
                # consolidation tasks (many deps) need more time to read files,
                # stitch and write — give them extra headroom.
                timeout = self.task_timeout_seconds
                if timeout and len(task.deps) >= 2:
                    timeout = max(timeout, self.task_timeout_seconds * 3)
                if timeout:
                    result = await asyncio.wait_for(
                        _run_task(), timeout=timeout
                    )
                else:
                    result = await _run_task()
            except asyncio.TimeoutError:
                # Degrade with whatever the executor already produced — a real
                # partial draft, not a placeholder — so dependents and the final
                # report can still assemble something useful.
                partial = collected[-1] if collected else ""
                task.status = "done"
                task.result = partial or (
                    f"⚠ task timed out after {self.task_timeout_seconds}s — "
                    "no content was produced before the timeout"
                )
                task.confidence = 0.4 if partial else 0.3
                gov.record_step(task, task.result, True)
                self._emit("task_timeout", {"id": task.id, "seconds": self.task_timeout_seconds})
                self._emit("task_done", {"id": task.id, "status": task.status, "confidence": task.confidence})
                return True
            except Exception as exc:  # executor crash → one retry, then escalate
                task.status = "pending" if task.retries < self.max_retries else "needs_human"
                task.result = f"executor error: {exc}"
                task.retries += 1
                self._emit("task_result", {"id": task.id, "error": str(exc), "status": task.status})
                return True

            self._emit("task_result", {"id": task.id, "result": result[:2000]})
            verdict = await self._review(task, result)
            self._emit(
                "task_review",
                {
                    "id": task.id,
                    "accepted": verdict.accepted,
                    "confidence": verdict.confidence,
                    "reason": verdict.reason,
                    "needs_human": verdict.needs_human,
                },
            )
            task.confidence = verdict.confidence
            if verdict.accepted:
                task.status = "done"
                task.result = result
            elif verdict.needs_human or task.retries >= self.max_retries:
                task.status = "needs_human"
                task.result = result
                task.retries += 1
            else:
                if self.controller is not None:
                    # G2: reviewer rejected — hold for the command deck's approval
                    # before re-running (reject or timeout → escalate to human).
                    self._emit(
                        "task_requeue_waiting",
                        {
                            "id": task.id,
                            "attempt": task.retries + 1,
                            "reason": verdict.reason,
                        },
                    )
                    approved = await self.controller.await_requeue(
                        task.id,
                        {"attempt": task.retries + 1, "reason": verdict.reason},
                        timeout=self.requeue_approval_timeout,
                    )
                    if approved:
                        task.status = "pending"  # re-run next round
                        self._emit(
                            "task_requeue_approved",
                            {"id": task.id, "attempt": task.retries + 1},
                        )
                    else:
                        # Skip/decline: accept the current result as-is (degraded,
                        # low confidence) so dependents can proceed. A skipped task
                        # must NOT deadlock the swarm — observed: skipping t1 left
                        # t2..t5 blocked forever, run stuck at 0/6 needs_human.
                        task.status = "done"
                        task.confidence = min(float(verdict.confidence or 0), 0.4)
                        self._emit(
                            "task_requeue_declined",
                            {"id": task.id, "reason": verdict.reason},
                        )
                else:
                    task.status = "pending"  # requeue for another attempt
                task.result = result
                task.retries += 1
            gov.record_step(task, result, verdict.accepted)
            self._emit("task_done", {"id": task.id, "status": task.status, "confidence": task.confidence})
            if task.status == "done":
                mem.add(
                    f"{task.description}\n→ {task.result[:500]}",
                    task_id=task.id,
                    run_token=run_token,
                    phase=task_phase(task, plan),
                )
            return True

        # Iterate until convergence: all tasks done, a task escalated to human,
        # the governance loop paused the run, or no progress is possible.
        while not plan.all_done() and not plan.needs_human() and not governance_paused:
            # G2 command deck: hold while paused, and feed operator directives into
            # this round's task hints so a stuck worker gets the operator's steer.
            ctrl = self.controller
            if ctrl is not None:
                await ctrl.wait_if_paused()
                directives = ctrl.drain_messages()
            else:
                directives = []
            # Governance inspection BEFORE dispatch (every N steps): red lines must
            # block a task before it runs; drift/viscosity checks the live plan.
            if self._runs % gov.config.check_every == 0:
                cmd = gov.inspect(plan)
                gov_log.append(f"[step {self._runs}] {cmd.action}: {cmd.reason} {cmd.metrics}")
                self._emit("governance", {"step": self._runs, "action": cmd.action, "reason": cmd.reason, "metrics": cmd.metrics})
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
            # Soft-budget deadline: once exhausted we run THIS final batch (tasks
            # that are ready now — e.g. the consolidation task whose deps just
            # finished) but schedule no further batches after it.
            budget_exhausted = deadline is not None and time.monotonic() >= deadline
            if budget_exhausted:
                self._emit(
                    "run_timed_out",
                    {"seconds": self.timeout_seconds, "final_batch": [t.id for t in ready[: max(1, self.max_parallel)]]},
                )
            batch = ready[: max(1, self.max_parallel)]
            done_before = {t.id for t in plan.tasks if t.done}
            results_before = {
                t.id: (t.result or "")[:200] for t in plan.tasks if t.result
            }
            results = await asyncio.gather(*(process(t) for t in batch))
            # T4 convergence guard: a round only "progresses" if something real
            # changed (a new done task, a changed result, or an accepted requeue
            # that flips a task back to pending). Repeated no-op rounds mean the
            # loop has reached a fixed point without completing the plan — that
            # is *stalled*, and we stop instead of spinning against the timeout.
            done_after = {t.id for t in plan.tasks if t.done}
            results_after = {
                t.id: (t.result or "")[:200] for t in plan.tasks if t.result
            }
            progressed = bool(done_after - done_before) or results_after != results_before
            if progressed:
                stall_rounds = 0
            else:
                stall_rounds += 1
                if stall_rounds >= self.stall_rounds_threshold:
                    stalled_reason = (
                        f"no progress for {self.stall_rounds_threshold} consecutive "
                        f"rounds (done={len(done_after)}/{len(plan.tasks)})"
                    )
                    self._emit("run_stalled", {"reason": stalled_reason})
                    break
            if not any(results):
                break
            # Soft budget exhausted: the final batch ran — stop scheduling new ones.
            if budget_exhausted:
                break

        status = (
            "completed"
            if plan.all_done()
            else "paused"
            if governance_paused or budget_exhausted
            else "stalled"
            if stalled_reason is not None
            else "needs_human"
            if plan.needs_human()
            else "failed"
        )
        summary = "\n\n".join(
            f"[{t.id}] {t.description}\n{t.result}" for t in plan.tasks if t.result
        )
        if stalled_reason is not None:
            gov_log.append(f"[stalled] {stalled_reason}")
        if budget_exhausted:
            gov_log.append(
                f"[step {self._runs}] TIMEOUT after {self.timeout_seconds}s "
                f"(done {sum(1 for t in plan.tasks if t.done)}/{len(plan.tasks)})"
            )
        self._emit("run_completed", {"status": status, "runs": self._runs})
        result = OrchestrationResult(
            intent=intent,
            plan=plan,
            summary=summary,
            status=status,
            runs=self._runs,
            governance_report="\n".join(gov_log),
        )
        self._persist_report(result)
        return result

    def _persist_report(self, result: OrchestrationResult) -> None:
        """Write the assembled deliverable to the workspace so the swarm ALWAYS
        produces a file, even when the consolidator never got around to writing it.

        If the intent names an explicit output file (\"写入 probe_test_1.md\" /
        \"保存为 report.txt\"), that file in the workspace root is used; otherwise a
        timestamped slug under _swarm_reports/."""
        import re as _re
        import time as _time

        report = result.final_report().strip()
        if not report or (report.startswith("⚠ task timed out") and len(report) < 40):
            return
        # honor an explicit output filename in the intent, if any
        m = _re.search(
            r"(?:写入|保存(?:到|为)?|输出(?:到|为)?|生成|创建|落盘(?:到)?|文件(?:名)?[:：]?)\s*"
            r"([\w\u4e00-\u9fff.\-]+\.(?:md|markdown|txt))",
            result.intent,
        )
        try:
            if m:
                fname = _re.sub(r"[\\/]+", "_", m.group(1))
                path = Path(self.workspace) / fname
            else:
                slug = _re.sub(r"[^\w\u4e00-\u9fff-]+", "_", result.intent)[:48].strip("_") or "report"
                out_dir = Path(self.workspace) / "_swarm_reports"
                out_dir.mkdir(parents=True, exist_ok=True)
                path = out_dir / f"{_time.strftime('%Y%m%d-%H%M%S')}-{slug}.md"
            # BEFORE overwriting an existing file, keep a timestamped backup so
            # "who overwrote whom" stays traceable when two swarm runs target the
            # same filename (dual-path A/B tests, retries, panel+main races).
            if path.exists():
                try:
                    backup_dir = Path(self.workspace) / "_swarm_reports" / "backups"
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    backup_path = backup_dir / f"{path.stem}.{_time.strftime('%Y%m%d-%H%M%S')}{path.suffix}"
                    backup_path.write_bytes(path.read_bytes())
                except Exception:
                    pass  # backup is best-effort; never block the deliverable write
            path.write_text(report, encoding="utf-8")
            # verify the write actually landed with the right content — an
            # executor's "claimed success" must never mask an empty/hollow file.
            if path.read_text(encoding="utf-8").strip() != report.strip():
                path.write_text(report, encoding="utf-8")
            result.report_path = str(path)
            self._emit("report_saved", {"path": str(path), "status": result.status})
        except OSError:
            pass
