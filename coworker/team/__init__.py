"""QunWork team / swarm organization module.

Team orchestration sits on top of the existing orchestrator, memory and inbox
layers. The module adds:
- agent_pool.py   — Agent instance state pool (idle / working / fault) + load tracking.
- store.py        — SQLite persistence for team members, agent instances, task groups.
- lifecycle.py    — Task group lifecycle state machine (forming → active → reviewing → dissolved).
- permission.py   — Role × capability matrix + fund approval thresholds (Phase 2).
- sync.py         — P2P asset sync engine (Phase 3).

All modules are safe to import when the team subsystem isn't configured — the
Manager creates a TeamSupport instance only when team data exists OR when a
front-end explicitly enables team mode.
"""

from .agent_pool import AgentPool, AgentInstance, AgentState
from .store import TeamStore
from .lifecycle import TaskLifecycle

__all__ = [
    "AgentPool",
    "AgentInstance",
    "AgentState",
    "TeamStore",
    "TaskLifecycle",
]
