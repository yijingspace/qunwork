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
import logging
import time
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..pheromone import StigmergyBus

from .governance import ESCALATE, NOP, PAUSE, REVERT, WARN, Governance, GovernanceCommand, GovernanceConfig
from .memory_store import PersistentVectorMemory
from .mesh import HexGrid, _mesh_mode_flags, bft_vote, claim_idle_agent, review_neighborhood, select_batch_grid, topology_health
from .models import OrchestrationResult, Plan, ReviewVerdict, Task

# The placeholder an executor leaves when a task timed out before producing any
# content (see the timeout-degrade path). Shared so every check that needs to
# distinguish "timeout placeholder" from a real deliverable uses ONE string.
_TASK_TIMEOUT_PREFIX = "⚠ task timed out"
from .vectormemory import VectorMemory
from .workers import (
    _run_engine_async,
    build_executor_engine,
    build_planner_engine,
    build_reviewer_engine,
)

logger = logging.getLogger(__name__)

_MAX_RETRIES_DEFAULT = 2


def task_phase(task: Any, plan: Any) -> int:
    """T5 periodic memory slot for a task: its ordinal in the plan, mod the
    Pisano period 60 (π(10)). Tasks at the same phase (e.g. the 5th task of
    every ~60-task run) share same-phase history in the memory pool — the
    DPNN closed-loop idea applied to swarm memory reuse."""
    try:
        # bug #3 (owner-audit 2026-08-07): list.index() compares with dataclass
        # __eq__ — two tasks with identical fields collide and get the wrong
        # phase slot. Identity (`is`) is the correct ordinal source.
        idx = next(i for i, t in enumerate(plan.tasks) if t is task)
    except (StopIteration, AttributeError):
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
    # fallback 2: explicit bullet / numbered list lines only — a bare year
    # prefix like "2024 年数据…" must NOT be treated as a task (bug #17).
    lines = [
        re.sub(r"^\s*[-*\u2022]|^\s*\d+[.)]\s+", "", ln).strip()
        for ln in text.splitlines()
        if re.match(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", ln) and len(ln.strip()) > 10
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
            logger.debug("clean_thought: reviewer JSON parse failed", exc_info=True)
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
            logger.debug("clean_thought: planner JSON parse failed", exc_info=True)
    # artifact/file links -> bare text
    t = re.sub(r"\[([^\]]*)\]\((?:artifact|file|attachment):[^)]*\)", r"\1", t)
    # code fences -> placeholder
    t = re.sub(r"```[a-zA-Z]*\n.*?```", "［代码已省略］", t, flags=re.DOTALL)
    # delivery-shell header line ("**Task [t0] 交付…**")
    t = re.sub(r"^\**\s*Task\s*\[[^\]]*\]\s*[^*\n]*\**\s*\n", "", t)
    return t[:400]


def _truncate_with_warning(text: str, limit: int) -> str:
    """截断长文本时给下游显式预警 (展示用途: GUI 事件流)。

    产物无损原则 (S3 修复): 评审者输入已改为完整传递 (不截断) — 本函数只
    用于 GUI 事件等*展示*场景, 截断时带标记 (总长/被裁量), 避免界面把
    残缺内容误读为完整产物。
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    head = text[:limit]
    total = len(text)
    return (
        f"{head}\n"
        f"\n…[展示截断: 原文 {total} 字符, 此处仅预览前 {limit} 字符 — "
        f"完整内容已保存, 产物无损]…"
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
    # Interconnect (asset loop): the TEAM memory store and the unified knowledge
    # DB are threaded into every worker engine — executors get remember/update/
    # forget against the team memory panel, and knowledge_search hits the same
    # library the /v1/knowledge API writes (no path split).
    memory_store: Optional[Any] = None
    knowledge_db_path: Optional[str] = None
    # #2 cognitive-action loop: HORNET resonator injects knowledge topology
    # context into planning and receives execution feedback to modulate phases.
    hornet_resonator: Optional[Any] = None
    # Refine 机制 (自进化闭环, 对标 Prime Agent Continual Harness): 持久化的
    # 蜂群经验库。提供时: _plan 注入历史经验上下文; run 结束后自动蒸馏本次
    # 经验回写 (成功策略/教训/技能提示/任务模板)。
    harness: Optional[Any] = None
    # Refine 是否在每次 run 后自动蒸馏 (默认开; 测试/headless 可关)。
    refine_auto: bool = True
    max_parallel: int = 4  # how many independent tasks run concurrently
    timeout_seconds: Optional[int] = 600  # whole-run timeout (None = no limit)
    task_timeout_seconds: Optional[int] = 240  # per-task timeout; timeout degrades to a partial result
    event_sink: Optional[Callable[[str, dict], None]] = None  # (kind, payload) progress feed
    # Per-turn token ledger sink forwarded to every worker engine (see TurnEngine.usage_sink).
    usage_sink: Optional[Callable[[dict, None]]] = None  # noqa: E501
    # G2 command deck: external control (pause/resume/operator message/requeue
    # approval). None = headless run with auto-requeue (legacy behaviour).
    controller: Optional[Any] = None
    requeue_approval_timeout: float = 120.0
    # P0 建议3: an already-built plan to execute instead of calling the planner
    # (the swarm deck's fork action copies the parent run's plan_ready + injects).
    initial_plan: Optional[Plan] = None
    # P0 增量1 (信息素负载均衡): shared stigmergic load field. Tasks deposit a
    # busy signal on start and withdraw on completion; the scheduler reads it to
    # shrink the parallel batch when the field is loaded. None = legacy behaviour.
    pheromone: Optional[Any] = None
    # P1 增量 (Agent 状态池 acquire/release): 如果 Manager 已实例化 AgentPool，
    # 这里就能用到它——None → 保持旧行为 (每次 _execute 都新建 build_engine)。
    agent_pool: Optional[Any] = None
    task_group_id: Optional[str] = None
    # QunMesh M2 (丝瓜络拓扑): 邻域感知批选择替换 ready[:max_parallel] 截断 —
    # 任务按 agent 负载信息素梯度流向空闲邻域 + 同 agent 批内名额上限 (分散)。
    # False = 旧行为 (回滚开关 mesh_scheduling, 与研究方案 M2 一致)。
    mesh_scheduling: bool = False
    # QunMesh M3 (角色邻域化): mesh_review=True → reviewer 注入四信道邻域上下文
    # (result/risk top-k 就近对照) + 低置信裁决触发 swarm_bft 三票聚合;
    # mesh_claim=True → agent_pool 同 role 无空闲时跨 role 领取 (谁近谁领, 消热点)。
    mesh_review: bool = False
    mesh_claim: bool = False
    # QunMesh M4 (全拓扑化): mesh_mode 四档总开关 off/serial/hybrid/full —
    # 按档位推导三开关 (显式 bool 与 mode 取或); full 额外启用每轮拓扑遥测
    # (λ₂ 网格代数连通度 + 热点迁徙建议, emit mesh_topology 事件)。
    mesh_mode: str = "off"
    # T4 convergence guard (LoopCoop fixed-point): how many consecutive rounds
    # without real progress (no new done task, no changed result, no accepted
    # requeue) before the run is declared stalled instead of spinning forever.
    stall_rounds_threshold: int = 2
    # 7x24 长程任务 (突破方案二): LoopCoop 收敛监控 — when provided, every
    # orchestration round records the convergence curve; the final report is
    # emitted as a "convergence_report" event (谱隙 |λ₂|, 理论收敛轮数, 实测曲线).
    # None = legacy behaviour (no convergence telemetry).
    loopcoop: Optional[Any] = None
    _runs: int = field(default=0, init=False)
    _run_seq: int = field(default=0, init=False)
    _last_plan: Optional[Plan] = field(default=None, init=False)
    _hornet_last_hits: list = field(default_factory=list, init=False)
    _hornet_last_phase: list = field(default_factory=list, init=False)
    # 自造工具蒸馏: 本次 run 中 executor 调用的工具 (tool_used 事件捕获),
    # run 结束时传给 refine_run 做经验蒸馏。
    _tool_uses: list = field(default_factory=list, init=False)
    # S12 临时文件治理: run 开始时间, 结束时清理此期间产生的临时文件。
    _run_started_at: float = field(default=0.0, init=False)
    # S10 治理信号链加固: 可选审计 sink — 治理命令 (PAUSE/REVERT/WARN/ESCALATE)
    # 写入持久化审计 (audit log 完整性), 安全干预可追溯。
    audit_sink: Optional[Callable[[dict[str, Any]], None]] = None

    def __post_init__(self) -> None:
        # QunMesh M4: mesh_mode 四档总开关推导 — 显式 bool 与档位取或
        # (off/serial/hybrid/full → scheduling/claim/review; full 另启拓扑遥测)。
        mode_flags = _mesh_mode_flags(self.mesh_mode)
        self.mesh_scheduling = bool(self.mesh_scheduling) or mode_flags["scheduling"]
        self.mesh_claim = bool(self.mesh_claim) or mode_flags["claim"]
        self.mesh_review = bool(self.mesh_review) or mode_flags["review"]
        self._mesh_topology_enabled = self.mesh_mode.strip().lower() == "full"

    # -- S6 失败模式蒸馏辅助 --------------------------------------------------
    def _failure_modes(self, limit: int = 5) -> list[dict[str, Any]]:
        """本次 run 的工具失败模式 (来自进程级 failure_mode 库, S8)。
        供 Refine 蒸馏成"失败模式教训"经验。best-effort。"""
        try:
            from ..tools.failure_mode import get_failure_registry

            return get_failure_registry().failure_modes()[:limit]
        except Exception:
            return []

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.event_sink is not None:
            try:
                self.event_sink(kind, payload)
            except Exception:
                logger.exception("event_sink %s failed", kind)

    def _pher_deposit(self, key: str, amount: float, *, channel: str = "load",
                      payload: Optional[str] = None) -> None:
        """QunMesh 生产端挂钩 (失败安全): 四信道信息素只进 StigmergyBus
        (PheromoneField 无四信道 kwargs); 写失败绝不影响编排主流程。"""
        if self.pheromone is None or not isinstance(self.pheromone, StigmergyBus):
            return
        try:
            self.pheromone.deposit(key, amount, channel=channel, payload=payload)
        except Exception:
            logger.exception("pheromone deposit %s/%s failed", channel, key)

    def _select_batch(self, ready: list) -> list:
        """P0 增量1 (信息素负载均衡): choose this round's parallel batch. Without a
        pheromone field this is the legacy `ready[:max_parallel]`. With one, the
        scheduler reads the stigmergic load signal: when the field is loaded
        (sum of live busy signals ≥ max_parallel), the batch shrinks proportionally
        so the colony never over-parallelizes against its own load.

        QunMesh M2 (mesh_scheduling=True): 邻域感知批选择 — 负载梯度引导 +
        同 agent 名额上限 (mesh.select_batch_grid), 失败安全退回本方法的
        信息素收缩路径。"""
        n = max(1, self.max_parallel)
        if self.pheromone is not None and len(ready) > 1:
            if self.mesh_scheduling:
                try:
                    return select_batch_grid(
                        ready, self.pheromone, n, executor_agent=self.executor_agent
                    )
                except Exception:
                    logger.exception("mesh batch select failed; falling back")
            try:
                load = self.pheromone.total_load()
            except Exception:
                load = 0.0
            if load >= self.max_parallel:
                shrink = self.max_parallel / max(1.0, load)
                n = max(1, int(self.max_parallel * shrink))
        return ready[:n]

    def _worker_feed(self, worker: str, task_id: str = "") -> Callable[[str, dict], None]:
        """Wrap a worker engine's on_event into sink events (chain-of-thought feed),
        normalized for display: raw JSON, code, artifact links and delivery shells
        are distilled into readable lines."""

        def feed(kind: str, payload: dict[str, Any]) -> None:
            if kind in ("worker_thought", "tool_thought"):
                self._emit(
                    "worker_thought",
                    {
                        "worker": worker,
                        "task_id": task_id,
                        "text": clean_thought(str(payload.get("text", "")), worker),
                    },
                )
            elif kind == "decision_trace":
                # 13 影子模式: 把 worker engine 的决策轨迹透传给 SwarmView。
                self._emit(
                    "decision_trace",
                    {
                        "worker": worker,
                        "task_id": task_id,
                        "entry": payload,
                    },
                )

        return feed

    # -- planning -----------------------------------------------------------
    async def _plan(self, intent: str) -> Plan:
        # #2 cognitive-action loop (forward): inject HORNET resonance context.
        # The planner sees which knowledge nodes resonate with the intent —
        # long-range associations from the hive topology seed the plan.
        resonance_ctx = ""
        if self.hornet_resonator is not None:
            try:
                res = self.hornet_resonator.resonate(intent, k=5)
                hits = res.get("hits", [])
                if hits:
                    lines = [f"  - {h['title']} (共振振幅: {h.get('amplitude', 0):.2f})" for h in hits]
                    resonance_ctx = "[HORNET 共振上下文 — 以下知识节点与任务意图产生共振]\n" + "\n".join(lines) + "\n\n"
                    self._hornet_last_hits = [h.get("node_id") for h in hits if h.get("node_id")]
                    self._hornet_last_phase = res.get("query_phase", [])
                else:
                    self._hornet_last_hits = []
                    self._hornet_last_phase = []
            except Exception:
                self._hornet_last_hits = []
                self._hornet_last_phase = []
        else:
            self._hornet_last_hits = []
            self._hornet_last_phase = []

        planner_input = resonance_ctx + intent if resonance_ctx else intent
        # Refine 机制 (自进化): 注入与意图相关的历史蜂群经验 — 上次跑同类
        # 任务的教训与成功策略, 让本次规划直接站在前人的肩膀上 (与 HORNET
        # 共振上下文同机制)。
        if self.harness is not None:
            try:
                from .refine import harness_context

                exp_ctx = harness_context(self.harness, intent, k=5)
                if exp_ctx:
                    planner_input = planner_input + "\n" + exp_ctx
            except Exception:
                pass
        last_err: Exception | None = None
        for attempt in range(3):  # planner JSON can be flaky — retry before giving up
            try:
                engine = build_planner_engine(
                    workspace=self.workspace,
                    provider=self.provider,
                    model=self.model,
                    model_settings=self.model_settings,
                    usage_sink=self.usage_sink,
                )
                text, status = await _run_engine_async(engine, planner_input, on_event=self._worker_feed("planner"))
                if not text:
                    last_err = RuntimeError(f"planner produced no plan (status: {status})")
                    continue
                return parse_plan(text, goal=intent)
            except (ValueError, RuntimeError) as exc:
                last_err = exc
                self._emit("planner_retry", {"attempt": attempt + 1, "error": str(exc)})
        raise RuntimeError(f"planner failed after 3 attempts: {last_err}")

    # -- execution ----------------------------------------------------------
    def _build_executor_engine(self, task: Task, *, role_tag: Optional[str] = None) -> Any:
        """Construct the executor TurnEngine for one task (agent pool aside).

        Extracted from _execute so a task-timeout run can pre-warm the engine
        BEFORE the timeout window starts (building spawns a shell + loads
        skills/knowledge — seconds of work that must not count against the
        task's execution budget)."""
        from . import auto_approver
        from .workers import build_executor_engine as _build_executor_engine

        role_tag = role_tag or task.agent or self.executor_agent
        return _build_executor_engine(
            workspace=self.workspace,
            provider=self.provider,
            model=self.model,
            approver=self.approver if self.approver is not None else auto_approver(),
            agent=role_tag,
            model_settings=self.model_settings,
            memory_store=self.memory_store,
            knowledge_db_path=self.knowledge_db_path,
            usage_sink=self.usage_sink,
        )

    async def _execute(
        self,
        task: Task,
        *,
        deps: list[str] = None,
        hints: list[str] = None,
        on_text: Optional[Callable[[str], None]] = None,
    ) -> str:
        from . import auto_approver

        role_tag = task.agent or self.executor_agent
        pool_inst = None
        acquired_agent_id: Optional[str] = None
        # 1) Try the pool first; if it gives us an instance, tag the group+task.
        if self.agent_pool is not None:
            try:
                pool_inst = self.agent_pool.acquire(
                    role_tag,
                    task_group_id=self.task_group_id,
                    task_id=task.id,
                )
                if pool_inst is not None:
                    acquired_agent_id = pool_inst.id
                elif self.mesh_claim:
                    # QunMesh M3 动态领取: 同 role 无空闲实例 → 跨 role 取低负载
                    # 实例 (丝瓜络「谁近谁领」消热点)。role_tag 不变 — 执行引擎的
                    # persona 由任务携带, 实例只是执行槽位 (节点无角色); 记账按
                    # 实例 id, release 走既有 finally 路径。失败安全回退旧行为。
                    pool_inst, claim_ev = claim_idle_agent(
                        self.agent_pool, role_tag, task_id=task.id,
                        task_group_id=self.task_group_id,
                    )
                    if pool_inst is not None:
                        acquired_agent_id = pool_inst.id
                        self._emit("mesh_claim", {"id": task.id, **claim_ev})
            except Exception:
                pool_inst = None
        try:
            # Consume a pre-warmed engine if the caller staged one (built OUTSIDE
            # the task-timeout window — see _process_impl). Always cleared so a
            # stale warm engine can never leak across tasks.
            engine = getattr(self, "_warm_executor", None)
            self._warm_executor = None
            if engine is None:
                engine = self._build_executor_engine(task, role_tag=role_tag)
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
                        on_text(raw)
                    self._emit(
                        "worker_thought",
                        {
                            "worker": "executor",
                            "task_id": task.id,
                            "agent_id": acquired_agent_id,
                            "text": clean_thought(raw, "executor"),
                        },
                    )
                elif kind == "tool_used":
                    # 自造工具蒸馏: 记录 executor 调用的工具 (含 create_selfmade_tool)
                    # 与该任务的关系 — run 结束后 refine 蒸馏"自造工具策略"经验。
                    name = payload.get("name") or ""
                    status = payload.get("status") or ""
                    if name and status == "started":
                        self._tool_uses.append(
                            {
                                "tool": name,
                                "task_id": task.id,
                                "task_desc": getattr(task, "description", ""),
                            }
                        )
                elif kind == "tool_thought" and payload.get("text"):
                    # Tool heartbeat — show it on the deck but NEVER collect it
                    # as a draft (the timeout-degrade path uses collected[-1]
                    # as the deliverable fragment).
                    self._emit(
                        "worker_thought",
                        {
                            "worker": "executor",
                            "task_id": task.id,
                            "agent_id": acquired_agent_id,
                            "text": clean_thought(str(payload["text"]), "executor"),
                        },
                    )
                elif kind == "decision_trace":
                    # 13 影子模式: executor 的决策轨迹透传给 SwarmView。
                    self._emit(
                        "decision_trace",
                        {
                            "worker": "executor",
                            "task_id": task.id,
                            "agent_id": acquired_agent_id,
                            "entry": payload,
                        },
                    )
                if acquired_agent_id is not None and self.agent_pool is not None:
                    try:
                        self.agent_pool.heartbeat(acquired_agent_id)
                    except Exception:
                        pass

            text, status = await _run_engine_async(engine, prompt, on_event=feed)
        finally:
            # 2) Always release back. The pool tolerates double release safely.
            if acquired_agent_id is not None and self.agent_pool is not None:
                try:
                    self.agent_pool.release(acquired_agent_id)
                except Exception:
                    pass
            # 3) Always reap the executor's resources — every build spawned a
            # persistent shell process (LocalExecutor.__init__) that would
            # otherwise leak per task (C2). Runs even when the task timed out
            # and wait_for cancelled _run_engine_async mid-stream.
            if engine is not None:
                close_exec = getattr(engine, "executor", None)
                if close_exec is not None:
                    close = getattr(close_exec, "close", None)
                    if close is not None:
                        try:
                            close()
                        except Exception:
                            pass
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
        """QunMesh M3 (mesh_review=True): reviewer 注入四信道邻域上下文
        (result/risk top-k 就近对照); 低置信边缘票 (<0.5) 额外拉 2 票独立复审,
        swarm_bft 多数决聚合 (prepare→commit, 平票保守拒绝)。False = 完全旧行为。

        Reviewer JSON can be flaky — retry before giving up, mirroring the
        planner's 3-attempt policy. A single malformed verdict must NEVER crash
        the whole asyncio.gather batch (owner-audit 2026-08-07: bug #2)."""
        neighborhood = review_neighborhood(self.pheromone) if self.mesh_review else ""
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                engine = build_reviewer_engine(
                    workspace=self.workspace,
                    provider=self.provider,
                    model=self.model,
                    model_settings=self.model_settings,
                    usage_sink=self.usage_sink,
                )
                parts = [
                    f"Task [{task.id}]: {task.description}",
                    f"Executor's result (完整产物, 不截断):\n{result}",
                ]
                if neighborhood:
                    parts.append(
                        "Neighborhood context (QunMesh nearby signals):\n" + neighborhood
                    )
                if attempt:
                    parts.append(f"(Independent re-review attempt {attempt + 1} — judge afresh.)")
                parts.append("Validate the result against the task. Return the JSON verdict.")
                prompt = "\n\n".join(parts)
                text, status = await _run_engine_async(
                    engine, prompt, on_event=self._worker_feed("reviewer", task.id)
                )
                if not text:
                    # No verdict → treat as accepted with low confidence rather than looping forever.
                    return ReviewVerdict(accepted=True, reason=f"no verdict (status: {status})", confidence=0.3)
                verdict = parse_verdict(text)
                # swarm_bft: 低置信接受是边缘票 — prepare(2 票独立复审) → commit(多数决)。
                # 预算保护: 仅低置信触发 (+2 次评审), 拒绝票走既有重排队通道不加票。
                if (
                    self.mesh_review
                    and verdict.accepted
                    and float(verdict.confidence or 0.0) < 0.5
                ):
                    votes = [verdict]
                    for extra in range(2):
                        try:
                            v_engine = build_reviewer_engine(
                                workspace=self.workspace,
                                provider=self.provider,
                                model=self.model,
                                model_settings=self.model_settings,
                                usage_sink=self.usage_sink,
                            )
                            v_prompt = prompt + (
                                f"\n\n(BFT vote {extra + 2}/{3} — independent judgment, ignore earlier reviews.)"
                            )
                            v_text, _v_status = await _run_engine_async(
                                v_engine, v_prompt, on_event=self._worker_feed("reviewer", task.id)
                            )
                            if v_text:
                                votes.append(parse_verdict(v_text))
                        except (ValueError, RuntimeError):
                            continue
                    merged, bft_meta = bft_vote(votes)
                    self._emit(
                        "review_bft",
                        {"id": task.id, "accepted": merged.accepted, "confidence": merged.confidence,
                         "reason": merged.reason, "needs_human": merged.needs_human, **bft_meta},
                    )
                    return merged
                return verdict
            except (ValueError, RuntimeError) as exc:
                last_err = exc
                self._emit("reviewer_retry", {"id": task.id, "attempt": attempt + 1, "error": str(exc)})
        # Degrade to a low-confidence accept instead of raising — a malformed
        # reviewer reply must not deadlock the swarm (asymmetric handling fixed).
        return ReviewVerdict(
            accepted=True,
            reason=f"reviewer parse failed after retries: {last_err}",
            confidence=0.2,
        )

    # -- main loop ----------------------------------------------------------
    async def run(self, intent: str) -> OrchestrationResult:
        # S12: 记录 run 开始时间 — 结束时清理此期间产生的临时文件。
        self._run_started_at = time.time()
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
            if report.strip() and not report.startswith(_TASK_TIMEOUT_PREFIX):
                result.status = "completed"
        # S12 临时文件治理: 清理本次 run 产生的临时/中间产物 (白名单保护
        # 正式报告/自造工具/状态库), best-effort, 不影响结果。显式豁免
        # 本次 run 的正式输出报告 (名字再像临时也不删)。
        try:
            from .temp_cleanup import cleanup_workspace_temp_files

            cleaned = cleanup_workspace_temp_files(
                self.workspace,
                since=self._run_started_at or None,
                keep=[result.report_path] if result.report_path else None,
            )
            if cleaned["count"]:
                logger.info(
                    "temp cleanup removed %d workspace temp file(s) (S12)",
                    cleaned["count"],
                )
        except Exception:
            logger.debug("temp cleanup skipped (best-effort)", exc_info=True)
        return result

    async def _run(self, intent: str, deadline: Optional[float] = None) -> OrchestrationResult:
        self._emit("run_started", {"intent": intent})
        # 7x24 长程任务 (突破方案二): 收敛监控器 — 每轮记录收敛度曲线。
        monitor = self.loopcoop
        if monitor is None:
            try:
                from .convergence import LoopCoopMonitor

                monitor = LoopCoopMonitor()
            except Exception:
                monitor = None
        # G2: operator directives accumulate per scheduling round (see while-loop).
        directives: list[str] = []
        # T4: consecutive no-progress rounds → stall (fixed point without completion).
        stall_rounds = 0
        stalled_reason: Optional[str] = None
        plan: Optional[Plan] = None
        planner_timed_out = False
        if self.initial_plan is not None:
            plan = self.initial_plan
        else:
            # The global timeout budget must constrain the PLANNER too — otherwise a
            # slow plan call eats the whole budget before the first task is even
            # dispatched (regression: test_orchestrator_timeout_pauses). Wait for
            # the plan within the remaining budget; on expiry return a paused run
            # with no plan instead of completing an empty plan.
            remaining = (deadline - time.monotonic()) if deadline is not None else None
            try:
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("planner budget exhausted before start")
                if remaining is not None:
                    plan = await asyncio.wait_for(self._plan(intent), timeout=remaining)
                else:
                    plan = await self._plan(intent)
            except TimeoutError:
                planner_timed_out = True
                plan = Plan(goal=intent, tasks=[])
                self._emit(
                    "run_timed_out",
                    {"seconds": self.timeout_seconds, "stage": "plan", "final_batch": []},
                )
        self._last_plan = plan
        # QunMesh 生产端: 规划落定 → task 招领信息素 (谁近谁领的梯度源)。
        for t in plan.tasks:
            self._pher_deposit(t.id, 1.0, channel="task", payload=(t.description or "")[:120])
        self._emit(
            "plan_ready",
            {
                "goal": intent,
                "tasks": [
                    {
                        "id": t.id,
                        "description": t.description,
                        "deps": t.deps,
                        "agent": t.agent,
                    }
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
            _own_mem = False  # caller owns lifecycle; do not close here
        elif self.memory_scope is not None:
            db = self.memory_db or str(Path(self.workspace) / ".qunwork" / "memory.db")
            mem = PersistentVectorMemory(db, scope=self.memory_scope, embedder=self.embedder)
            _own_mem = True  # owner-audit 2026-08-07 (bug #4): we created it, we close it
        else:
            mem = VectorMemory(embedder=self.embedder)
            _own_mem = True
        # C12: the SQLite-backed memory handle must be closed even when _run
        # raises mid-flight (executor/governance exceptions). The explicit close
        # at the end covers the happy path; this registers the SAME close as a
        # finalizer so the abnormal path is covered too. `self` may outlive the
        # run, but the mem object itself becomes unreachable when this run's
        # locals die — weakref on the STORE (not self) fires at that point.
        mem_close = getattr(mem, "close", None)
        if _own_mem and mem_close is not None:
            try:
                import weakref
                weakref.finalize(mem, mem_close)
            except Exception:
                pass
        gov_log: list[str] = []
        governance_paused = False
        budget_exhausted = False  # set when the soft deadline passes; stops new batches

        async def process(task: Task) -> bool:
            """Pheromone-wrapped task runner: deposit a busy signal on start,
            withdraw on completion (finally) — even on executor crashes."""
            pher_key = task.agent or self.executor_agent
            if self.pheromone is not None:
                self.pheromone.deposit(pher_key, 1.0)
            try:
                return await _process_impl(task)
            finally:
                if self.pheromone is not None:
                    self.pheromone.deposit(pher_key, -1.0)

        async def _process_impl(task: Task) -> bool:
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

            # Pre-warm the executor engine OUTSIDE the task-timeout window: building
            # spawns a shell process and loads skills/knowledge (seconds on this
            # machine), so counting it against a short task_timeout would burn the
            # whole budget before the first draft is ever produced. Handed to
            # _execute via an instance slot (NOT a keyword arg) so tests that stub
            # _execute keep their signatures; _execute consumes and clears it.
            warm_engine = None
            if self.task_timeout_seconds:
                try:
                    warm_engine = self._build_executor_engine(task)
                except Exception:
                    warm_engine = None
            self._warm_executor = warm_engine

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
                    f"{_TASK_TIMEOUT_PREFIX} after {self.task_timeout_seconds}s — "
                    "no content was produced before the timeout"
                )
                task.confidence = 0.4 if partial else 0.3
                # bug #11: a timeout is NOT an accepted step — marking it True
                # inflated the governance autonomy ratio.
                gov.record_step(task, task.result, False)
                self._emit("task_timeout", {"id": task.id, "seconds": self.task_timeout_seconds})
                self._emit("task_done", {"id": task.id, "status": task.status, "confidence": task.confidence})
                return True
            except Exception as exc:  # executor crash → one retry, then escalate
                task.status = "pending" if task.retries < self.max_retries else "needs_human"
                task.result = f"executor error: {exc}"
                task.retries += 1
                # bug #10: a crashed attempt is still a recorded step — without
                # this the governance autonomy ratio ignores failed work.
                gov.record_step(task, task.result, False)
                # QunMesh: executor 崩溃 → risk 信息素 (枢纽重规划梯度源)。
                self._pher_deposit(
                    task.id, 1.0, channel="risk", payload=f"executor error: {exc}"[:120]
                )
                self._emit("task_result", {"id": task.id, "error": str(exc), "status": task.status})
                return True

            # GUI 事件流展示用截断 (完整结果已存 task.result, 产物无损);
            # 截断时带预警, 避免界面误读为完整内容。
            self._emit(
                "task_result",
                {
                    "id": task.id,
                    "result": _truncate_with_warning(result, 2000),
                },
            )
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
                # QunMesh: 评审拒绝 → risk 信息素 (强度 ∝ 不确信度)。
                self._pher_deposit(
                    task.id,
                    max(0.2, 1.0 - float(verdict.confidence or 0.0)),
                    channel="risk",
                    payload=(verdict.reason or "")[:120],
                )
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
            # QunMesh: 任务终结 → done/needs_human 撤 task 招领 + result 通知
            # (下游依赖与就近评审的梯度源); pending (重排队下一轮重跑) 招领保留。
            if task.status != "pending":
                self._pher_deposit(task.id, -1.0, channel="task")
            if task.status == "done":
                self._pher_deposit(
                    task.id, 1.0, channel="result", payload=(task.result or "")[:120]
                )
            self._emit("task_done", {"id": task.id, "status": task.status, "confidence": task.confidence})
            if task.status == "done":
                # S9 并行协作去重: worker 结果写入 blackboard 前去重
                # (多个 worker 并行时避免重复探测/重复记忆膨胀)。
                try:
                    mem.add_deduped(
                        f"{task.description}\n→ {task.result[:500]}",
                        task_id=task.id,
                        run_token=run_token,
                        phase=task_phase(task, plan),
                    )
                except Exception:
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
                # P0 建议3 (蜂群指挥台): drain structured DAG edits — fork-task
                # injections + pending-task agent retargets. `by_id` is captured
                # by the process() closure by NAME, so rebinding it here makes
                # ready()/dependency lookups see the new tasks immediately.
                # getattr-guarded: a minimal test Deck that only implements the
                # G2 basics must keep working.
                for spec in getattr(ctrl, "drain_task_injections", lambda: [])():
                    tid = spec.get("id", "")
                    if not tid or tid in by_id:
                        continue  # duplicate id — drop
                    deps = spec.get("deps") or []
                    if any(d not in by_id for d in deps):
                        continue  # unknown dep would deadlock — drop
                    plan.tasks.append(
                        Task(
                            id=tid,
                            description=spec.get("description", ""),
                            deps=list(deps),
                            agent=spec.get("agent", ""),
                        )
                    )
                    by_id = plan.by_id()
                    stall_rounds = 0  # a fresh task is real progress — reset the stall guard
                    self._emit(
                        "task_injected",
                        {
                            "id": tid,
                            "description": spec.get("description", ""),
                            "deps": list(deps),
                            "agent": spec.get("agent", ""),
                        },
                    )
                for spec in getattr(ctrl, "drain_retargets", lambda: [])():
                    tgt = by_id.get(spec.get("id", ""))
                    if tgt is not None and tgt.status == "pending":
                        tgt.agent = spec.get("agent", "")
                        self._emit(
                            "task_retargeted", {"id": tgt.id, "agent": tgt.agent}
                        )
            else:
                directives = []
            # Governance inspection BEFORE dispatch (every N steps): red lines must
            # block a task before it runs; drift/viscosity checks the live plan.
            # Owner-audit 2026-08-07 (bug #1): compute ready FIRST so drift()
            # measures the task about to be dispatched, not plan.tasks[-1].
            ready = [t for t in plan.tasks if t.ready(by_id)]
            # bug #18: gate by completed STEPS (len(gov.steps)), not by loop
            # rounds (self._runs) — batch size distorted "every N steps" into
            # "every N rounds". 0 % N == 0 keeps the first-round check.
            if len(gov.steps) % gov.config.check_every == 0:
                cmd = gov.inspect(plan, current_task=ready[0] if ready else None)
                gov_log.append(f"[step {self._runs}] {cmd.action}: {cmd.reason} {cmd.metrics}")
                self._emit("governance", {"step": self._runs, "action": cmd.action, "reason": cmd.reason, "metrics": cmd.metrics})
                # S10 治理信号链加固: 治理命令写入持久化审计 (audit log 完整性,
                # 安全干预可追溯)。best-effort — 审计失败不阻断治理。
                if self.audit_sink is not None:
                    try:
                        self.audit_sink(
                            {
                                "event": "governance",
                                "run_id": f"run_{self._run_seq}",
                                "step": self._runs,
                                "action": cmd.action,
                                "reason": cmd.reason,
                                "metrics": cmd.metrics,
                                "ts": time.time(),
                            }
                        )
                    except Exception:
                        pass
                if cmd.action == REVERT:
                    tgt = gov.revert_target(plan)
                    if tgt is not None:
                        gov_log.append(f"[step {self._runs}] REVERT -> re-dispatch {tgt.id}")
                        tgt.status = "pending"
                        tgt.result = ""
                        tgt.confidence = 0.0
                        # Owner-audit 2026-08-07 (bug #8): reset retries so the
                        # re-dispatched task gets a full retry budget instead of
                        # immediately hitting max_retries → needs_human.
                        tgt.retries = 0
                        # REVERT changed a done task back to pending — recompute
                        # ready so the reverted task is picked up THIS round.
                        ready = [t for t in plan.tasks if t.ready(by_id)]
                elif cmd.action == WARN:
                    gov.note_warning(cmd)
                elif cmd.action in (PAUSE, ESCALATE):
                    gov.note_warning(cmd)
                    governance_paused = True
                    break

            if not ready:
                blocked = [t.id for t in plan.tasks if t.status == "pending"]
                # M7: diagnose DANGLING deps — a pending task whose deps reference
                # task ids that don't exist (planner hallucination / JSON drift)
                # would otherwise deadlock the run into a silent 'failed' with zero
                # events. Emit a run-stalled-style diagnostic so callers know why.
                dangling = [
                    {
                        "id": t.id,
                        "missing_deps": [
                            d for d in t.deps if d not in by_id
                        ],
                    }
                    for t in plan.tasks
                    if t.status == "pending" and any(d not in by_id for d in t.deps)
                ]
                if dangling:
                    reason = (
                        f"{len(dangling)} task(s) blocked by unknown dependency ids: "
                        + ", ".join(
                            f"{d['id']}→{d['missing_deps']}" for d in dangling[:5]
                        )
                    )
                    stalled_reason = stalled_reason or reason
                    self._emit("run_stalled", {"reason": reason, "dangling": dangling})
                    gov_log.append(f"[stalled] {reason}")
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
            batch = self._select_batch(ready)
            # QunMesh M4 (mesh_mode=full): 每轮拓扑遥测 — λ₂ 网格代数连通度
            # (LoopCoop 谱隙的网格级观测) + 热点迁徙建议。只读旁路, 不影响调度。
            if self._mesh_topology_enabled:
                try:
                    topo = topology_health(self.pheromone)
                    self._emit("mesh_topology", {"round": self._runs, **topo})
                except Exception:
                    logger.exception("mesh_topology emit failed")
            done_before = {t.id for t in plan.tasks if t.done}
            results_before = {
                t.id: (t.result or "")[:200] for t in plan.tasks if t.result
            }
            results = await asyncio.gather(*(process(t) for t in batch))
            # 7x24 长程任务 (突破方案二): 每轮记录收敛度 (done+accepted 比例)。
            if monitor is not None:
                try:
                    monitor.record_round(plan)
                except Exception:
                    logger.exception("loopcoop record_round failed")
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
            if budget_exhausted:
                break

        status = (
            "paused"
            if planner_timed_out
            else "completed"
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
        if planner_timed_out:
            gov_log.append(
                f"[plan] TIMEOUT: planner exceeded {self.timeout_seconds}s budget — "
                "no plan, no deliverable"
            )
            summary = (
                f"⚠ run timed out: planner produced no plan within "
                f"{self.timeout_seconds}s budget — no deliverable"
            )
        if stalled_reason is not None:
            gov_log.append(f"[stalled] {stalled_reason}")
        if budget_exhausted:
            gov_log.append(
                f"[step {self._runs}] TIMEOUT after {self.timeout_seconds}s "
                f"(done {sum(1 for t in plan.tasks if t.done)}/{len(plan.tasks)})"
            )
        self._emit("run_completed", {"status": status, "runs": self._runs})
        # 7x24 长程任务 (突破方案二): 输出收敛报告事件 — 谱隙 |λ₂|、理论轮数、
        # 实测收敛曲线与最终收敛度。best-effort, 监控失败不影响运行结果。
        if monitor is not None:
            try:
                self._emit("convergence_report", monitor.report())
            except Exception:
                logger.exception("convergence_report emit failed")
        result = OrchestrationResult(
            intent=intent,
            plan=plan,
            summary=summary,
            status=status,
            runs=self._runs,
            governance_report="\n".join(gov_log),
        )
        if not planner_timed_out:
            self._persist_report(result)
        # Owner-audit 2026-08-07 (bug #4): close SQLite-backed memory we
        # created for this run so connections don't leak across many runs.
        if _own_mem:
            close = getattr(mem, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        # #2 cognitive-action loop (reverse): feed execution result back to
        # HORNET — success crystallizes the resonance attractor (phase nudge
        # toward query phase), failure pushes activated cells to Z- traceback.
        if self.hornet_resonator is not None and self._hornet_last_hits:
            try:
                success = status == "completed"
                self.hornet_resonator.store.feedback(
                    self._hornet_last_hits,
                    success=success,
                    query_phase=self._hornet_last_phase or None,
                )
            except Exception:
                pass
        # Refine 机制 (自进化闭环): 运行结束后自动把本次经验蒸馏进 harness —
        # 成功策略/教训/技能提示/任务模板, 下次同类任务直接受益。best-effort,
        # 绝不因蒸馏失败影响运行结果返回。
        if self.refine_auto and self.harness is not None and not planner_timed_out:
            try:
                from .refine import refine_run

                # source run 标记: 有 run_store 时用真实 run_id 不可得 (result
                # 不含), 用自增序号标记本次运行, 足以审计"这条经验来自哪次跑"。
                if not hasattr(result, "run_id"):
                    result.run_id = f"run_{self._run_seq}"  # type: ignore[attr-defined]
                refined = refine_run(
                    result,
                    self.harness,
                    # 自造工具蒸馏: 本次 run executor 调用的工具清单。
                    tool_uses=list(self._tool_uses),
                    # S6 失败模式蒸馏: 本次 run 反复失败的工具 (failure_mode 库)。
                    failure_modes=self._failure_modes(),
                )
                n_added = len(refined.get("added", []))
                if n_added:
                    self._emit(
                        "refined",
                        {
                            "added": n_added,
                            "existing": len(refined.get("existing", [])),
                        },
                    )
            except Exception:
                logger.exception("refine_run failed (best-effort)")
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
        if not report or (report.startswith(_TASK_TIMEOUT_PREFIX) and len(report) < 40):
            return        # honor an explicit output filename in the intent, if any
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
                    logger.debug("report backup failed (best-effort)", exc_info=True)
            path.write_text(report, encoding="utf-8")
            # verify the write actually landed with the right content — an
            # executor's "claimed success" must never mask an empty/hollow file.
            if path.read_text(encoding="utf-8").strip() != report.strip():
                path.write_text(report, encoding="utf-8")
            result.report_path = str(path)
            self._emit("report_saved", {"path": str(path), "status": result.status})
        except OSError as exc:
            # Owner-audit 2026-08-07 (bug #13): never silently swallow the
            # report write failure — emit an event + log so the caller knows
            # report_path is empty because the write failed, not because there
            # was no report.
            logger.warning("report persist failed: %s", exc, exc_info=True)
            self._emit("report_save_failed", {"error": str(exc), "status": result.status})
