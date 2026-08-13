"""P1-5 零信任能力袋 (Least Privilege for Agents).

每个连接器工具声明所需 scope; Persona 角色默认只授予最小 scope 集合;
运行时越权自动升级为 Inbox 审批而非直接放行。

Scope 体系遵循 OAuth 风格的 `{level}:{resource}` 格式:
  - read:repo / write:pr / admin:repo  (GitHub)
  - read:channel / write:message / admin:invite  (Slack)
  - read:user / write:approval / admin:group  (企业微信/钉钉/飞书)

三级层次: read < write < admin, 高级 scope 隐含低级。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

# Scope 层级: 高级隐含低级 (admin > write > read)
SCOPE_LEVELS = {"read": 1, "write": 2, "admin": 3}


def scope_level(scope: str) -> int:
    """提取 scope 的层级数字 (read=1, write=2, admin=3)."""
    prefix = scope.split(":", 1)[0]
    return SCOPE_LEVELS.get(prefix, 0)


def scope_implies(granted: str, required: str) -> bool:
    """granted scope 是否隐含 required scope.

    同 resource 下高级别隐含低级别: write:repo 隐含 read:repo;
    不同 resource 不隐含: write:repo 不隐含 read:channel。
    """
    g_parts = granted.split(":", 1)
    r_parts = required.split(":", 1)
    if len(g_parts) != 2 or len(r_parts) != 2:
        return granted == required
    g_level, g_res = g_parts
    r_level, r_res = r_parts
    if g_res != r_res:
        return False
    return scope_level(g_level) >= scope_level(r_level)


# -- 连接器工具 scope 声明表 ----------------------------------------------
# 每个连接器的工具 → 所需 scope 列表。工具名格式: {connector}__{tool} 或 mcp__{name}__{tool}
# 未声明的工具默认 read 级别 (最小权限原则的反向: 未声明不阻断读)

CONNECTOR_SCOPES: dict[str, dict[str, list[str]]] = {
    "github": {
        "list_repos": ["read:repo"],
        "get_repo": ["read:repo"],
        "list_issues": ["read:repo"],
        "create_issue": ["write:issue"],
        "comment_issue": ["write:issue"],
        "create_pr": ["write:pr"],
        "merge_pr": ["admin:pr"],
        "review_pr": ["write:pr"],
        "list_commits": ["read:repo"],
        "create_branch": ["write:repo"],
        "delete_branch": ["admin:repo"],
    },
    "gitlab": {
        "list_projects": ["read:project"],
        "create_issue": ["write:issue"],
        "create_pr": ["write:mr"],
        "merge_pr": ["admin:mr"],
    },
    "slack": {
        "list_channels": ["read:channel"],
        "post_message": ["write:message"],
        "invite_user": ["admin:invite"],
        "list_users": ["read:user"],
    },
    "telegram": {
        "send_message": ["write:message"],
        "get_chat": ["read:chat"],
        "send_interactive": ["write:message"],
    },
    "jira": {
        "get_issue": ["read:issue"],
        "create_issue": ["write:issue"],
        "transition_issue": ["write:issue"],
        "assign_issue": ["write:issue"],
        "delete_issue": ["admin:issue"],
    },
    # -- 国内连接器 ----------------------------------------------------------
    "wecom": {  # 企业微信
        "get_user": ["read:user"],
        "send_message": ["write:message"],
        "create_group": ["admin:group"],
        "get_department": ["read:department"],
        "approve_approval": ["write:approval"],
        "create_approval": ["write:approval"],
    },
    "dingtalk": {  # 钉钉
        "get_user": ["read:user"],
        "send_message": ["write:message"],
        "create_group": ["admin:group"],
        "get_department": ["read:department"],
        "approve_approval": ["write:approval"],
        "start_process": ["write:process"],
    },
    "feishu": {  # 飞书 / Lark
        "get_user": ["read:user"],
        "send_message": ["write:message"],
        "create_group": ["admin:group"],
        "get_department": ["read:department"],
        "approve_approval": ["write:approval"],
        "create_doc": ["write:doc"],
        "read_doc": ["read:doc"],
    },
    "notion": {
        "get_page": ["read:page"],
        "create_page": ["write:page"],
        "update_page": ["write:page"],
        "delete_page": ["admin:page"],
    },
    "linear": {
        "get_issue": ["read:issue"],
        "create_issue": ["write:issue"],
        "update_issue": ["write:issue"],
    },
    "asana": {
        "get_task": ["read:task"],
        "create_task": ["write:task"],
        "update_task": ["write:task"],
    },
}


def get_tool_scopes(tool_name: str) -> list[str]:
    """从 tool_name 解析出所需 scope 列表。

    tool_name 格式:
      - {connector}__{tool}  (内置连接器)
      - mcp__{server}__{tool}  (MCP 连接器)
      - {tool}  (内置工具如 load_skill, 无连接器)
    """
    if "__" not in tool_name:
        return []  # 内置工具无 scope 约束

    parts = tool_name.split("__", 2)
    if parts[0] == "mcp" and len(parts) >= 3:
        connector = parts[1]
        tool = parts[2]
    elif len(parts) >= 2:
        connector = parts[0]
        tool = parts[1]
    else:
        return []

    scopes = CONNECTOR_SCOPES.get(connector, {})
    # fail-closed (零信任): 未在 CONNECTOR_SCOPES 声明的工具(含所有 MCP 工具、
    # 新连接器)默认 write 级 → check_scope 升级为审批, 而非静默放行。
    return scopes.get(tool, ["write:default"])


def parse_connector_from_tool(tool_name: str) -> Optional[str]:
    """从 tool_name 提取 connector id."""
    if "__" not in tool_name:
        return None
    parts = tool_name.split("__", 2)
    if parts[0] == "mcp" and len(parts) >= 3:
        return parts[1]
    return parts[0]


class PersonaScopeStore:
    """持久化每个 Persona 角色被授予的 scope 集合。

    Schema: persona_id × connector → [scope, ...]
    未配置的 connector 默认授予 read 级别 (最小可用)。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(self._path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._con.execute(
                """
                CREATE TABLE IF NOT EXISTS persona_scopes (
                    persona_id TEXT NOT NULL,
                    connector TEXT NOT NULL,
                    scopes TEXT NOT NULL DEFAULT '[]',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (persona_id, connector)
                )
                """
            )
            self._con.commit()

    def get_scopes(self, persona_id: str, connector: str) -> list[str]:
        """读取 persona 在某 connector 上被授予的 scope 列表。

        未配置时返回 None (调用方决定默认策略)。
        """
        with self._lock:
            row = self._con.execute(
                "SELECT scopes FROM persona_scopes WHERE persona_id=? AND connector=?",
                (persona_id, connector),
            ).fetchone()
        if not row:
            return []
        try:
            return json.loads(row["scopes"])
        except (json.JSONDecodeError, TypeError):
            return []

    def get_all_scopes(self, persona_id: str) -> dict[str, list[str]]:
        """读取 persona 在所有 connector 上的 scope 配置."""
        with self._lock:
            rows = self._con.execute(
                "SELECT connector, scopes FROM persona_scopes WHERE persona_id=?",
                (persona_id,),
            ).fetchall()
        return {r["connector"]: json.loads(r["scopes"]) for r in rows}

    def set_scopes(
        self, persona_id: str, connector: str, scopes: list[str]
    ) -> None:
        import time as _time

        with self._lock:
            self._con.execute(
                """
                INSERT INTO persona_scopes (persona_id, connector, scopes, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(persona_id, connector)
                DO UPDATE SET scopes=excluded.scopes, updated_at=excluded.updated_at
                """,
                (persona_id, connector, json.dumps(scopes), _time.time()),
            )
            self._con.commit()

    def remove_scopes(self, persona_id: str, connector: str) -> None:
        with self._lock:
            self._con.execute(
                "DELETE FROM persona_scopes WHERE persona_id=? AND connector=?",
                (persona_id, connector),
            )
            self._con.commit()

    def list_personas(self) -> list[str]:
        with self._lock:
            rows = self._con.execute(
                "SELECT DISTINCT persona_id FROM persona_scopes"
            ).fetchall()
        return [r["persona_id"] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._con.close()


def check_scope(
    persona_id: str,
    tool_name: str,
    store: PersonaScopeStore,
) -> "ScopeDecision":
    """核心检查: persona 是否有权调用 tool_name。

    返回 ScopeDecision:
      - allowed=True: scope 匹配, 直接放行
      - allowed=False, needs_approval=True: 越权, 升级为 Inbox 审批
      - allowed=True, warned=True: 未声明 scope, 放行但标记 (开发期诊断)
    """
    required_scopes = get_tool_scopes(tool_name)
    if not required_scopes:
        return ScopeDecision(allowed=True, reason="no scope required")

    connector = parse_connector_from_tool(tool_name)
    if not connector:
        return ScopeDecision(allowed=True, reason="builtin tool")

    granted = store.get_scopes(persona_id, connector)
    if not granted:
        # 未配置 scope → 默认只允许 read 级别
        max_required_level = max(scope_level(s) for s in required_scopes)
        if max_required_level <= 1:
            return ScopeDecision(allowed=True, reason="default read access")
        return ScopeDecision(
            allowed=False,
            needs_approval=True,
            reason=f"persona '{persona_id}' has no scope config for '{connector}'; "
            f"tool requires {required_scopes} (write/admin needs explicit grant)",
            required_scopes=required_scopes,
            connector=connector,
        )

    # 逐个检查: 每个 required scope 必须被某个 granted scope 隐含
    for req in required_scopes:
        if not any(scope_implies(g, req) for g in granted):
            return ScopeDecision(
                allowed=False,
                needs_approval=True,
                reason=f"scope '{req}' required but not granted "
                f"(granted: {granted})",
                required_scopes=required_scopes,
                granted_scopes=granted,
                connector=connector,
            )

    return ScopeDecision(
        allowed=True, reason="scope matched", granted_scopes=granted
    )


class ScopeDecision:
    """scope 检查结果."""

    def __init__(
        self,
        allowed: bool,
        reason: str = "",
        needs_approval: bool = False,
        required_scopes: Optional[list[str]] = None,
        granted_scopes: Optional[list[str]] = None,
        connector: Optional[str] = None,
    ) -> None:
        self.allowed = allowed
        self.reason = reason
        self.needs_approval = needs_approval
        self.required_scopes = required_scopes or []
        self.granted_scopes = granted_scopes or []
        self.connector = connector


def all_connector_scopes() -> dict[str, dict[str, list[str]]]:
    """返回完整的 scope 声明表 (供前端权限矩阵 UI 使用)."""
    return json.loads(json.dumps(CONNECTOR_SCOPES))  # deep copy
