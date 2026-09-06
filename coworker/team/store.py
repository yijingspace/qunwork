"""Team persistence layer — SQLite store for team metadata.

Tables:
- team              – one row (local team info + identity for sync)
- members           – roster of human/agent team members
- agent_instances   – persisted snapshot of the AgentPool (restored on startup)
- task_groups       – lifecycle state for swarms (forming/active/reviewing/dissolved)

SQLite is the right fit here:
* data stays next to usage.db / orchestration.db (one data dir concept)
* the team module is single-writer (team API guarded by Manager lock anyway)
* relational shape is simpler than splitting JSON stores + keeps atomic rollbacks
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from ..permission_matrix import ROLE_REGISTRY, normalize_role


# 2026-09-06 方案A 角色统一: 校验词汇表来自 permission_matrix.ROLE_REGISTRY
# (单一角色定义源, 与权限矩阵/团队 API/GUI 下拉共用), 历史别名 gm 经由
# normalize_role 自动映射 general_manager — 不再维护独立清单。
_VALID_MEMBER_ROLES = set(ROLE_REGISTRY)
_VALID_GROUP_STATES = {"forming", "active", "reviewing", "dissolved"}


def _now() -> float:
    return time.time()


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _json_loads(raw: Optional[str], default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


class TeamStore:
    """SQLite-backed team store. Safe for Manager's multi-thread usage."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    # ── schema --------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS team (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    my_member_id TEXT,
                    created_at REAL NOT NULL,
                    sync_key TEXT,
                    relay_url TEXT
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS members (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    persona_id TEXT,
                    status TEXT NOT NULL DEFAULT 'offline',
                    current_task_group TEXT,
                    last_seen REAL,
                    public_key TEXT,
                    invited_at REAL,
                    joined_at REAL
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS agent_instances (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'idle',
                    current_task_group TEXT,
                    current_task_id TEXT,
                    load REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    last_heartbeat REAL NOT NULL
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS task_groups (
                    group_id TEXT PRIMARY KEY,
                    owner_member TEXT,
                    goal TEXT NOT NULL,
                    member_ids TEXT NOT NULL,
                    agent_ids TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'forming',
                    created_at REAL NOT NULL,
                    dissolved_at REAL
                )"""
            )
            # P2P 同步变更日志 (TeamSync outbox/inbox)。
            # change_id 全局唯一(作者前缀) → 去重; ts 用于 LWW 合并。
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS sync_changes (
                    change_id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    op TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    ts REAL NOT NULL,
                    author TEXT NOT NULL,
                    synced INTEGER NOT NULL DEFAULT 0
                )"""
            )
            # 同步配置: peer 端点 + 上次同步时间
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS sync_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )
            # 2026-09-06 方案A 数据迁移: 历史角色别名 gm → general_manager
            # (幂等 — 旧值不存在时 0 行更新)。同版本代码在对端节点执行同样
            # 迁移, 两端自然一致, 无需额外同步变更。
            self._db.execute(
                "UPDATE members SET role='general_manager' WHERE role='gm'"
            )
            self._db.commit()

    # ── Team identity -------------------------------------------------------
    def ensure_team(self, name: str = "My Team") -> dict:
        """Idempotently create the local team row. Returns current team info."""
        with self._lock:
            row = self._db.execute("SELECT * FROM team LIMIT 1").fetchone()
            if row is None:
                team_id = f"team-{uuid.uuid4().hex[:12]}"
                member_id = f"member-{uuid.uuid4().hex[:10]}"
                now = _now()
                self._db.execute(
                    "INSERT INTO team(id,name,my_member_id,created_at) VALUES (?,?,?,?)",
                    (team_id, name, member_id, now),
                )
                # 自动把本机用户加为董事长
                self._db.execute(
                    """INSERT INTO members(id,name,role,status,invited_at,joined_at)
                       VALUES (?,?,?,?,?,?)""",
                    (member_id, "Me", "chairman", "online", now, now),
                )
                self._db.commit()
                return self._dict_team(team_id, name, member_id, now, None, None)
            return self._dict_team(*row)

    def get_team(self) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM team LIMIT 1").fetchone()
            if row is None:
                return None
            return self._dict_team(*row)

    @staticmethod
    def _dict_team(tid, name, my, created, sync_key, relay) -> dict:
        return {
            "id": tid,
            "name": name,
            "my_member_id": my,
            "created_at": created,
            "sync_key": sync_key,
            "relay_url": relay,
        }

    # ── Members -------------------------------------------------------------
    def add_member(
        self,
        name: str,
        role: str = "worker",
        *,
        persona_id: Optional[str] = None,
        public_key: Optional[str] = None,
    ) -> dict:
        role = normalize_role(role)
        if role not in _VALID_MEMBER_ROLES:
            raise ValueError(f"invalid member role: {role}")
        member_id = f"member-{uuid.uuid4().hex[:10]}"
        now = _now()
        with self._lock:
            self._db.execute(
                """INSERT INTO members(id,name,role,persona_id,status,public_key,invited_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (member_id, name, role, persona_id, "offline", public_key, now),
            )
            self._db.commit()
        return self.get_member(member_id) or {"id": member_id, "name": name, "role": role}

    def update_member(self, member_id: str, **fields) -> bool:
        allowed = {"name", "role", "persona_id", "status", "current_task_group", "last_seen", "public_key", "joined_at"}
        cols = [f for f in fields if f in allowed]
        if "role" in fields:
            fields["role"] = normalize_role(fields["role"])
            if fields["role"] not in _VALID_MEMBER_ROLES:
                raise ValueError(f"invalid member role: {fields['role']}")
        if not cols:
            return False
        stmt = ", ".join(f"{c} = ?" for c in cols)
        vals = [fields[c] for c in cols] + [member_id]
        with self._lock:
            self._db.execute(f"UPDATE members SET {stmt} WHERE id = ?", vals)
            self._db.commit()
            return True

    def remove_member(self, member_id: str) -> bool:
        with self._lock:
            self._db.execute("DELETE FROM members WHERE id = ?", (member_id,))
            self._db.commit()
            return True

    def get_member(self, member_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT id,name,role,persona_id,status,current_task_group,last_seen FROM members WHERE id=?",
                (member_id,),
            ).fetchone()
            return self._dict_member(row) if row else None

    def list_members(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id,name,role,persona_id,status,current_task_group,last_seen FROM members ORDER BY joined_at,invited_at"
            ).fetchall()
            return [self._dict_member(r) for r in rows]

    @staticmethod
    def _dict_member(row: tuple) -> dict:
        return {
            "id": row[0],
            "name": row[1],
            "role": row[2],
            "persona_id": row[3],
            "status": row[4],
            "current_task_group": row[5],
            "last_seen": row[6],
        }

    # ── Task groups ---------------------------------------------------------
    def create_task_group(
        self,
        goal: str,
        *,
        owner_member: Optional[str] = None,
        member_ids: Optional[list[str]] = None,
        agent_ids: Optional[list[str]] = None,
        group_id: Optional[str] = None,
    ) -> dict:
        gid = group_id or f"group-{uuid.uuid4().hex[:12]}"
        now = _now()
        member_ids = member_ids or []
        agent_ids = agent_ids or []
        with self._lock:
            self._db.execute(
                """INSERT INTO task_groups(group_id,owner_member,goal,member_ids,agent_ids,state,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    gid,
                    owner_member,
                    goal,
                    _json_dumps(member_ids),
                    _json_dumps(agent_ids),
                    "forming",
                    now,
                ),
            )
            self._db.commit()
        return self.get_task_group(gid) or {"group_id": gid, "goal": goal, "state": "forming"}

    def update_task_group_state(self, group_id: str, state: str) -> bool:
        if state not in _VALID_GROUP_STATES:
            raise ValueError(f"invalid task_group state: {state}")
        vals: list = [state]
        stmt = "state = ?"
        if state == "dissolved":
            stmt += ", dissolved_at = ?"
            vals.append(_now())
        vals.append(group_id)
        with self._lock:
            self._db.execute(f"UPDATE task_groups SET {stmt} WHERE group_id = ?", vals)
            self._db.commit()
            return True

    def add_member_to_group(self, group_id: str, member_id: str) -> bool:
        return self._add_to_group(group_id, "member_ids", member_id)

    def add_agent_to_group(self, group_id: str, agent_id: str) -> bool:
        return self._add_to_group(group_id, "agent_ids", agent_id)

    def _add_to_group(self, group_id: str, col: str, value: str) -> bool:
        with self._lock:
            row = self._db.execute(
                f"SELECT {col} FROM task_groups WHERE group_id = ?", (group_id,)
            ).fetchone()
            if not row:
                return False
            ids = _json_loads(row[0], [])
            if value in ids:
                return True
            ids.append(value)
            self._db.execute(
                f"UPDATE task_groups SET {col} = ? WHERE group_id = ?",
                (_json_dumps(ids), group_id),
            )
            self._db.commit()
            return True

    def get_task_group(self, group_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT group_id,owner_member,goal,member_ids,agent_ids,state,created_at,dissolved_at FROM task_groups WHERE group_id=?",
                (group_id,),
            ).fetchone()
            return self._dict_group(row) if row else None

    def list_task_groups(self, *, include_dissolved: bool = False) -> list[dict]:
        with self._lock:
            q = "SELECT group_id,owner_member,goal,member_ids,agent_ids,state,created_at,dissolved_at FROM task_groups"
            if not include_dissolved:
                q += " WHERE state != 'dissolved'"
            q += " ORDER BY created_at DESC"
            rows = self._db.execute(q).fetchall()
            return [self._dict_group(r) for r in rows]

    @staticmethod
    def _dict_group(row: tuple) -> dict:
        return {
            "id": row[0],                 # friendly alias for callers
            "group_id": row[0],           # keep backward compatibility with DB schema names
            "owner_member": row[1],
            "goal": row[2],
            "member_ids": _json_loads(row[3], []),
            "agent_ids": _json_loads(row[4], []),
            "state": row[5],
            "created_at": row[6],
            "dissolved_at": row[7],
        }

    # ── Persisted agent snapshots -------------------------------------------
    def save_agent_snapshot(self, instances) -> int:
        """Persist the AgentPool's current set for recovery after restart."""
        with self._lock:
            self._db.execute("DELETE FROM agent_instances")
            for a in instances:
                self._db.execute(
                    """INSERT INTO agent_instances(id,role,persona_id,state,current_task_group,current_task_id,load,created_at,last_heartbeat)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        a.id, a.role, a.persona_id, a.state.value,
                        a.current_task_group, a.current_task_id,
                        a.load, a.created_at, a.last_heartbeat,
                    ),
                )
            self._db.commit()
            return len(instances)

    def load_agent_snapshot(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id,role,persona_id,state,current_task_group,current_task_id,load,created_at,last_heartbeat FROM agent_instances"
            ).fetchall()
            return [
                {
                    "id": r[0], "role": r[1], "persona_id": r[2],
                    "state": r[3], "current_task_group": r[4],
                    "current_task_id": r[5], "load": r[6],
                    "created_at": r[7], "last_heartbeat": r[8],
                }
                for r in rows
            ]

    # ── misc ----------------------------------------------------------------
    def team_summary(self) -> Optional[dict]:
        """Quick bundle for /v1/team response (counts + status)."""
        team = self.get_team()
        if team is None:
            return None
        members = self.list_members()
        online_count = sum(1 for m in members if m["status"] == "online")
        # sync_status 在 Phase 3 由 sync.py 更新。这里返回 single — 相当于“尚未启用团队同步”。
        return {
            **{k: v for k, v in team.items() if k not in ("sync_key",)},
            "member_count": len(members),
            "online_count": online_count,
            "sync_status": "single",
            "last_sync": None,
        }

    # ── P2P sync (TeamSync outbox / config) ----------------------------------
    def record_sync_change(
        self,
        entity_type: str,
        entity_id: str,
        op: str,
        payload: dict,
        *,
        author: str = "local",
        change_id: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> str:
        """Append one change to the outbox. Returns its change_id."""
        cid = change_id or f"{author}:{uuid.uuid4().hex[:12]}"
        now = ts if ts is not None else _now()
        with self._lock:
            self._db.execute(
                """INSERT OR REPLACE INTO sync_changes(change_id,entity_type,entity_id,op,payload,ts,author,synced)
                   VALUES (?,?,?,?,?,?,?,0)""",
                (cid, entity_type, entity_id, op, _json_dumps(payload), now, author),
            )
            self._db.commit()
        return cid

    def pending_sync_changes(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT change_id,entity_type,entity_id,op,payload,ts,author FROM sync_changes WHERE synced=0 ORDER BY ts LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "change_id": r[0], "entity_type": r[1], "entity_id": r[2],
                "op": r[3], "payload": _json_loads(r[4], {}), "ts": r[5], "author": r[6],
            }
            for r in rows
        ]

    def mark_sync_changes_synced(self, change_ids: list[str]) -> int:
        if not change_ids:
            return 0
        with self._lock:
            cur = self._db.executemany(
                "UPDATE sync_changes SET synced=1 WHERE change_id=?",
                [(cid,) for cid in change_ids],
            )
            self._db.commit()
        return cur.rowcount

    def list_sync_changes(self, after_id: int = 0, limit: int = 500) -> list[dict]:
        """Full ordered change log (for a peer pulling our outbox)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT change_id,entity_type,entity_id,op,payload,ts,author FROM sync_changes WHERE change_id > ? ORDER BY ts LIMIT ?",
                (after_id, limit),
            ).fetchall()
        return [
            {
                "change_id": r[0], "entity_type": r[1], "entity_id": r[2],
                "op": r[3], "payload": _json_loads(r[4], {}), "ts": r[5], "author": r[6],
            }
            for r in rows
        ]

    def ingest_sync_change(self, change: dict) -> bool:
        """Idempotent insert of an incoming change (dedup by change_id)."""
        cid = str(change.get("change_id") or "")
        if not cid:
            return False
        with self._lock:
            exists = self._db.execute(
                "SELECT 1 FROM sync_changes WHERE change_id=?", (cid,)
            ).fetchone()
            if exists:
                return False
            self._db.execute(
                """INSERT INTO sync_changes(change_id,entity_type,entity_id,op,payload,ts,author,synced)
                   VALUES (?,?,?,?,?,?,?,1)""",
                (
                    cid,
                    str(change.get("entity_type") or ""),
                    str(change.get("entity_id") or ""),
                    str(change.get("op") or "upsert"),
                    _json_dumps(change.get("payload") or {}),
                    float(change.get("ts") or _now()),
                    str(change.get("author") or "remote"),
                ),
            )
            self._db.commit()
        return True

    def sync_config_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM sync_config WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else default

    def sync_config_set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO sync_config(key,value) VALUES (?,?)",
                (key, value),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()