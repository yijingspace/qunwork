"""Task-group lifecycle management.

对照文档「临时蜂群动态组网，任务完成即时解耦」:
  forming  → active  → reviewing → dissolved
              ↓ fault (auto-migration or inbox to human)

The lifecycle object bridges TeamStore (persistence) ↔ AgentPool (instance
pool) ↔ Orchestrator (actual execution). Manager wires the three together so
front-end API calls feel atomic.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from .agent_pool import AgentPool
from .store import TeamStore

logger = logging.getLogger(__name__)

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "forming": {"active", "dissolved"},
    "active": {"reviewing", "dissolved", "fault"},
    "reviewing": {"active", "dissolved"},
    # dissolved / fault 是终态，但 dissolve 从任意态都允许进入
}


class TaskLifecycle:
    """High-level task-group state machine."""

    def __init__(
        self,
        store: TeamStore,
        pool: AgentPool,
        *,
        ingest_swarm_assets: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.store = store
        self.pool = pool
        self._ingest = ingest_swarm_assets or (lambda group_id: None)

    # ── public API ---------------------------------------------------------
    def create(
        self,
        goal: str,
        *,
        owner_member: Optional[str] = None,
        member_ids: Optional[list[str]] = None,
        agent_ids: Optional[list[str]] = None,
        group_id: Optional[str] = None,
    ) -> dict:
        """Create a group in FORMING state — 组网阶段。"""
        return self.store.create_task_group(
            goal,
            owner_member=owner_member,
            member_ids=member_ids,
            agent_ids=agent_ids,
            group_id=group_id,
        )

    def transition(self, group_id: str, new_state: str) -> dict:
        """Validate and commit a state transition. Returns refreshed group."""
        current = self.store.get_task_group(group_id)
        if current is None:
            raise KeyError(f"unknown task_group: {group_id}")
        current_state = current["state"]
        # dissolve 是“强制终态”允许从任何地方进入
        if new_state != "dissolved" and not self._is_valid_transition(current_state, new_state):
            raise ValueError(
                f"invalid task_group transition {current_state} → {new_state}"
            )
        self.store.update_task_group_state(group_id, new_state)
        if new_state == "dissolved":
            self._cleanup_on_dissolve(group_id, current)
        return self.store.get_task_group(group_id) or {}

    def dissolve(self, group_id: str) -> dict:
        """Friendly API for the dissolve endpoint.
        Steps: 1) mark dissolved  2) release agent pool instances  3) ingest assets.
        """
        current = self.store.get_task_group(group_id)
        if current is None:
            raise KeyError(f"unknown task_group: {group_id}")
        if current["state"] == "dissolved":
            return {"ok": True, "already_dissolved": True, "group": current}

        # 1) 记录解散时间
        self.store.update_task_group_state(group_id, "dissolved")

        # 2) 把该 group 占用的 agent 全部归池
        for aid in current.get("agent_ids", []):
            try:
                self.pool.release(aid)
            except Exception:
                logger.exception("agent %s release during dissolve failed", aid)

        # 3) 归档到知识库
        try:
            self._ingest(group_id)
        except Exception:
            logger.exception("ingest_swarm_assets failed on %s", group_id)

        refreshed = self.store.get_task_group(group_id) or current
        return {"ok": True, "dissolved": True, "group": refreshed}

    # ── helpers -------------------------------------------------------------
    @staticmethod
    def _is_valid_transition(src: str, dst: str) -> bool:
        if src == "fault" and dst in ("active", "dissolved"):
            return True
        return dst in _VALID_TRANSITIONS.get(src, set())

    def _cleanup_on_dissolve(self, group_id: str, snapshot: dict) -> None:
        # Update all members' current_task_group pointer (detach).
        for mid in snapshot.get("member_ids", []):
            self.store.update_member(mid, current_task_group=None)
