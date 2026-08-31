"""Agent instance state pool.

对照文档「运营调度 Agent 按忙闲筛选闲置工蜂」——现有 orchestrator 在
_dispatch 时每次 build_engine，没有复用状态池。本模块补上资源管理面：
- acquire(release) 标记 idle / working
- heartbeat 记录最后心跳
- reap_stale 清理超时故障实例（可由 pheromone/迁移系统接管）
- load_summary 暴露给组织首页和调度器

The pool is *intentionally in-memory*: 状态需要实时读写，持久化由
store.TeamStore 负责（启动时恢复池、关机前快照）。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AgentState(Enum):
    IDLE = "idle"
    WORKING = "working"
    FAULT = "fault"


@dataclass
class AgentInstance:
    """One instantiated engine/worker currently held in the pool."""

    id: str
    role: str                      # planner / executor / reviewer / cowork / code / custom persona_id
    persona_id: str                # the persona passed to build_engine()
    state: AgentState = AgentState.IDLE
    current_task_group: Optional[str] = None
    current_task_id: Optional[str] = None
    load: float = 0.0              # 0.0 – 1.0 (rough indicator, used for balancing)
    created_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)

    @property
    def is_available(self) -> bool:
        return self.state == AgentState.IDLE and self.load < 0.8


class AgentPool:
    """Thread-safe registry of live Agent instances."""

    def __init__(self, max_per_role: Optional[dict[str, int]] = None) -> None:
        self._lock = threading.RLock()
        self._agents: dict[str, AgentInstance] = {}
        self._max_per_role: dict[str, int] = max_per_role or {}

    # ── lifecycle -----------------------------------------------------------
    def register(
        self,
        role: str,
        persona_id: str,
        agent_id: Optional[str] = None,
        *,
        working: bool = False,
        task_id: Optional[str] = None,
        task_group_id: Optional[str] = None,
    ) -> AgentInstance:
        """Register a new (pre-built) engine instance. Returns the added record.

        `working=True` (elastic provisioning): the instance is born occupied —
        without it, the second concurrent task's acquire() immediately grabs the
        freshly-registered IDLE instance and every task collapses onto one key
        (实测 0.21.2 run: 拓扑恒 1 节点 0 边)。`task_id` 与 working 配套:
        注册即上岗必须同时认领任务 — 否则 reap_stale 判 FAULT 后拿不到
        current_task_id, 无法定位并取消 inflight 协程 (节点故障拾取断链)。"""
        with self._lock:
            # Idle cap per role. Callers rely on the pool not silently refusing, so
            # we raise — it's better to surface an over-provisioning mistake early.
            cap = self._max_per_role.get(role)
            if cap is not None:
                current = sum(1 for a in self._agents.values() if a.role == role)
                if current >= cap:
                    raise RuntimeError(f"AgentPool: role '{role}' at capacity ({cap})")
            uid = agent_id or f"agent-{uuid.uuid4().hex[:10]}"
            inst = AgentInstance(id=uid, role=role, persona_id=persona_id)
            if working:
                inst.state = AgentState.WORKING
                inst.load = 0.3  # 与 acquire 的初始占用一致
            if task_id is not None:
                inst.current_task_id = task_id
                inst.current_task_group = task_group_id
            self._agents[uid] = inst
            return inst

    def unregister(self, agent_id: str) -> bool:
        """Remove an instance (operator-initiated deprovision)."""
        with self._lock:
            return bool(self._agents.pop(agent_id, None))

    # ── acquire / release ---------------------------------------------------
    def acquire(
        self,
        role: str,
        *,
        task_group_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Optional[AgentInstance]:
        """Get the best (lowest-load) idle agent for a role, or None."""
        with self._lock:
            candidates = [
                a for a in self._agents.values()
                if a.role == role and a.is_available
            ]
            if not candidates:
                return None
            # 优先低负载 → 最早创建（更稳定的实例）
            candidates.sort(key=lambda a: (a.load, a.created_at))
            agent = candidates[0]
            agent.state = AgentState.WORKING
            agent.current_task_group = task_group_id
            agent.current_task_id = task_id
            agent.load = min(1.0, agent.load + 0.3)
            return agent

    def release(self, agent_id: str) -> bool:
        """Mark idle after a task completes (load decays).
        M5: FAULT 实例不洗白 — reap 判死的节点由 cancel→release 路径回收卡槽
        (清 current_task/load), 但保持 FAULT 态, is_available 永不复活它。"""
        with self._lock:
            a = self._agents.get(agent_id)
            if a is None:
                return False
            if a.state != AgentState.FAULT:
                a.state = AgentState.IDLE
            a.current_task_group = None
            a.current_task_id = None
            a.load = max(0.0, a.load - 0.3)
            return True

    # ── housekeeping --------------------------------------------------------
    def heartbeat(self, agent_id: str) -> None:
        with self._lock:
            a = self._agents.get(agent_id)
            if a is not None:
                a.last_heartbeat = time.time()

    def reap_stale(self, timeout: float = 300) -> list[str]:
        """Mark agents without a heartbeat for >timeout as FAULT.
        Returns the list of newly-faulted IDs (orchestrator can trigger migration)."""
        now = time.time()
        with self._lock:
            stale = [
                aid for aid, a in self._agents.items()
                if now - a.last_heartbeat > timeout and a.state == AgentState.WORKING
            ]
            for aid in stale:
                self._agents[aid].state = AgentState.FAULT
            return stale

    def cleanup_faulty(self, timeout: float = 300) -> list[str]:
        """Convenience alias used by lifecycle & tests."""
        return self.reap_stale(timeout=timeout)

    # ── snapshots -----------------------------------------------------------
    def list(
        self,
        *,
        role: Optional[str] = None,
        state: Optional[AgentState] = None,
    ) -> list[AgentInstance]:
        with self._lock:
            items = list(self._agents.values())
        if role:
            items = [a for a in items if a.role == role]
        if state:
            items = [a for a in items if a.state == state]
        return items

    def get(self, agent_id: str) -> Optional[AgentInstance]:
        with self._lock:
            a = self._agents.get(agent_id)
            return AgentInstance(**a.__dict__) if a else None

    def load_summary(self) -> dict:
        """Readable summary for /v1/team/agents + UI top bar."""
        with self._lock:
            total = len(self._agents)
            by_role: dict[str, int] = {}
            by_state: dict[str, int] = {s.value: 0 for s in AgentState}
            avg_load = 0.0
            for a in self._agents.values():
                by_role[a.role] = by_role.get(a.role, 0) + 1
                by_state[a.state.value] += 1
                avg_load += a.load
            avg_load = round(avg_load / total, 3) if total else 0.0
        return {
            "total": total,
            "idle": by_state["idle"],
            "working": by_state["working"],
            "fault": by_state["fault"],
            "by_role": by_role,
            "avg_load": avg_load,
        }
