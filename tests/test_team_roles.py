"""方案A 角色统一: 注册表单一源 + gm 别名 + 存量数据迁移 (2026-09-06)."""

from __future__ import annotations

import sqlite3

import pytest

from coworker.permission_matrix import (
    MATRIX,
    ROLE_REGISTRY,
    can,
    fund_approval,
    normalize_role,
)
from coworker.team.store import TeamStore, _VALID_MEMBER_ROLES


def test_role_registry_covers_member_roles():
    """team 校验词汇表 == 注册表键 — 单一源, 不再有第二份清单。"""
    assert _VALID_MEMBER_ROLES == set(ROLE_REGISTRY)


def test_registry_roles_all_in_matrix():
    """注册表里每个角色都在权限矩阵中 — 杜绝 gm/scheduler/auditor 式 fail-closed 脱节。"""
    missing = [r for r in ROLE_REGISTRY if r not in MATRIX]
    assert missing == []


def test_gm_alias_normalized_everywhere():
    """历史别名 gm → general_manager: normalize + can + fund_approval 全链生效。"""
    assert normalize_role("gm") == "general_manager"
    assert normalize_role("worker") == "worker"
    assert normalize_role("unknown-x") == "unknown-x"
    # 修复前: can("gm", "read_memory") == False (fail-closed 全禁)
    assert can("gm", "read_memory") is True
    assert can("gm", "fund:general_manager") is True
    verdict = fund_approval("gm", 3_000)
    assert verdict["allowed"] is True


def test_scheduler_auditor_have_capabilities():
    """补进矩阵的两个角色: scheduler 可路由派单, auditor 只读+报告+警示。"""
    assert can("scheduler", "issue_commands:route")
    assert can("scheduler", "project_group")
    assert not can("scheduler", "fund:general_manager")
    assert can("auditor", "read_memory")
    assert can("auditor", "issue_commands:warn")
    assert not can("auditor", "issue_commands")


def test_store_accepts_registry_roles_and_rejects_unknown(tmp_path):
    """add/update_member 用注册表校验; gm 别名入库即规范化; 未知角色拒绝。"""
    s = TeamStore(tmp_path / "team.db")
    m = s.add_member("张三", "gm")
    assert m["role"] == "general_manager"  # 入库即规范化
    s.update_member(m["id"], role="auditor")
    assert s.get_member(m["id"])["role"] == "auditor"
    with pytest.raises(ValueError):
        s.add_member("李四", "supreme_leader")
    with pytest.raises(ValueError):
        s.update_member(m["id"], role="supreme_leader")


def test_store_migrates_legacy_gm_rows(tmp_path):
    """存量迁移: 已有 role='gm' 的行, 重开库自动改写 general_manager。"""
    db = tmp_path / "team.db"
    # 手工造一行 legacy 数据 (绕过新校验)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE members (id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,"
        " persona_id TEXT, status TEXT NOT NULL DEFAULT 'offline', current_task_group TEXT,"
        " last_seen REAL, public_key TEXT, invited_at REAL, joined_at REAL)"
    )
    con.execute(
        "INSERT INTO members(id,name,role,status) VALUES ('member-legacy','老成员','gm','offline')"
    )
    con.commit()
    con.close()

    s = TeamStore(db)  # _init_schema 触发迁移
    rows = s.list_members()
    assert rows[0]["role"] == "general_manager"
