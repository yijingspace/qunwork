"""Persistent memory — adapter interface + scopes.

Memory is the long-lived layer above transient conversation state: durable facts,
preferences, task notes, summaries. Scopes: global (user-wide), workspace (per project),
session. Backends are adapters (`SQLiteMemoryStore` now, `PostgresMemoryStore` later).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class Scope(str, Enum):
    GLOBAL = "global"
    WORKSPACE = "workspace"
    SESSION = "session"


@dataclass
class MemoryItem:
    id: int
    scope: Scope
    content: str
    key: Optional[str] = None
    workspace: Optional[str] = None
    session_id: Optional[str] = None
    created_at: Optional[str] = None
    use_count: int = 0
    stale: bool = False
    # P2 MemCube 式元数据 (MemOS MemCube): provenance/version/ttl/hotness。
    version: int = 1
    ttl: Optional[str] = None      # ISO 过期时间 (UTC); None = 永不过期
    origin: Optional[str] = None   # 来源标记 (e.g. "remember", "import", "auto")
    hotness: float = 0.0           # 热度 (0..1), 由使用频率派生


class MemoryStore(ABC):
    @abstractmethod
    def add(
        self,
        content: str,
        *,
        scope: Scope = Scope.WORKSPACE,
        key: Optional[str] = None,
        workspace: Optional[str] = None,
        session_id: Optional[str] = None,
        origin: Optional[str] = None,
        ttl: Optional[str] = None,
    ) -> MemoryItem: ...

    @abstractmethod
    def get(self, item_id: int) -> Optional[MemoryItem]: ...

    @abstractmethod
    def list(
        self,
        *,
        scope: Optional[Scope] = None,
        workspace: Optional[str] = None,
        session_id: Optional[str] = None,
        include_stale: bool = False,
    ) -> list[MemoryItem]: ...

    @abstractmethod
    def update(self, item_id: int, content: str) -> Optional[MemoryItem]: ...

    @abstractmethod
    def delete(self, item_id: int) -> bool: ...

    def mark_stale(self, item_id: int, stale: bool = True) -> bool:
        """P1 衰减遗忘: 标记/清除冷记忆的 stale 状态 (Manus"降级而非抹除")。
        基类提供默认 no-op, 具体适配器 (SQLite) 实现持久化。"""
        return False

    def bump_usage(self, item_id: int) -> None:
        """P1 衰减遗忘: 记忆被命中时刷新 use_count 与 last_used_at。
        基类提供默认 no-op。"""

    # -- P2 MemCube 式元数据 (MemOS MemCube) ----------------------------------
    def set_ttl(self, item_id: int, ttl: Optional[str]) -> bool:
        """设置/清除记忆的 TTL (ISO 过期时间)。基类默认 no-op。"""
        return False

    def history(self, item_id: int) -> list[dict[str, Any]]:
        """版本链: 返回该记忆的历史版本 (含当前版本)。基类默认返回空。"""
        return []

    def rollback(self, item_id: int, version: int) -> Optional[MemoryItem]:
        """回滚到指定版本 (版本链支持回滚)。基类默认 no-op。"""
        return None


def format_memories(items: list[MemoryItem]) -> str:
    """Render memories for injection into the system prompt. Ids are shown so the agent
    can revise a memory (`memory_update`) or retire it (`memory_forget`)."""
    if not items:
        return ""
    lines = [f"- [#{item.id}] {item.content}" for item in items]
    return "Known memories (from earlier sessions):\n" + "\n".join(lines)
