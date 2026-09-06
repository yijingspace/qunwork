"""方案D 权限矩阵可编辑 + 治理审计 (2026-09-06).

覆盖: 生效矩阵合并 / GUI toggle 计算 / store LWW / sync 传播 (确定性
change_id) / org gate 读覆盖 + deny 审计落点 / audit_log 分页。
"""

from __future__ import annotations

import pytest

from coworker.permission_matrix import (
    MATRIX,
    compute_override_toggle,
    effective_matrix,
    org_gate,
    register_audit_sink,
    register_matrix_provider,
    role_can_tool,
    role_caps,
)
from coworker.team.store import TeamStore
from coworker.team.sync import TeamSync


@pytest.fixture(autouse=True)
def _isolate_globals():
    yield
    register_matrix_provider(None)
    register_audit_sink(None)


# -- 生效矩阵 -------------------------------------------------------------------


def test_effective_matrix_add_remove_scope_prefix():
    ov = {
        "worker": {"add": ["issue_commands"], "remove": [], "ts": 1},
        "chairman": {"add": [], "remove": ["project_group"], "ts": 1},
        "reviewer": {"add": [], "remove": ["write_memory"], "ts": 1},  # 前缀: 移除 write_memory:report
    }
    eff = effective_matrix(ov)
    assert "issue_commands" in eff["worker"]
    assert "project_group" not in eff["chairman"]
    assert "write_memory:report" not in eff["reviewer"]  # scope 变体被前缀移除
    assert "read_memory" in eff["reviewer"]
    # 纯函数路径不泄漏全局 provider
    assert role_caps("worker") == MATRIX["worker"]


def test_toggle_computation_four_paths():
    base = {"read_memory", "write_memory:business"}
    # grant 未持有 → add
    add, rm = compute_override_toggle(base, {}, "issue_commands", True)
    assert add == ["issue_commands"] and rm == []
    # grant 已被回收 → 从 remove 撤销 (回基线)
    add, rm = compute_override_toggle(base, {"remove": ["read_memory"]}, "read_memory", True)
    assert rm == [] and add == []
    # revoke 基线持有 → remove
    add, rm = compute_override_toggle(base, {}, "read_memory", False)
    assert rm == ["read_memory"] and add == []
    # revoke 覆盖获得 → 撤 add (不残留 remove)
    add, rm = compute_override_toggle(base, {"add": ["vote"]}, "vote", False)
    assert add == [] and rm == []
    # 幂等: 目标态已达 → 不变
    add, rm = compute_override_toggle(base, {"add": ["vote"]}, "vote", True)
    assert add == ["vote"] and rm == []


# -- store + provider -----------------------------------------------------------


def test_store_override_roundtrip_lww_and_provider(tmp_path):
    store = TeamStore(tmp_path / "team.db")  # __init__ 自动挂 provider
    assert effective_matrix()["worker"] == MATRIX["worker"]  # provider → {}
    r = store.toggle_matrix_capability("worker", "issue_commands", True, by="me")
    assert r and r["add"] == ["issue_commands"]
    # org gate 立即感知覆盖: worker 的 shell 从拦 → 放
    ok, _ = org_gate("worker", "run_shell", {"command": "ls"})
    assert ok is True
    assert role_can_tool("worker", "run_shell") is True
    # LWW: 更旧版本拒 (ingest 路径)
    applied = store.set_matrix_overrides(
        "worker", add=[], remove=["issue_commands"], ts=r["ts"] - 10, if_newer=True
    )
    assert applied is False
    assert "issue_commands" in role_caps("worker")
    # 空覆盖 = 回归代码底
    store.set_matrix_overrides("worker", add=[], remove=[], ts=r["ts"] + 10)
    assert store.get_matrix_overrides() == {}
    ok, _ = org_gate("worker", "run_shell", {"command": "ls"})
    assert ok is False


def test_invalid_role_rejected(tmp_path):
    store = TeamStore(tmp_path / "t.db")
    with pytest.raises(ValueError):
        store.set_matrix_overrides("supreme_leader", add=["vote"], remove=[], ts=1)
    store.close()


# -- sync 传播 -------------------------------------------------------------------


def _node(tmp_path, store_name, sync_name):
    store = TeamStore(tmp_path / f"{store_name}.db")
    sync = TeamSync(store, secrets_path=tmp_path / f"{sync_name}_secrets", author=sync_name)
    return store, sync


def test_matrix_overrides_sync_across_nodes(tmp_path):
    """A 端编辑矩阵 → 加密变更 → B 端合并生效; 旧版本被 LWW 拒。"""
    store_a, sync_a = _node(tmp_path, "team_a", "a")
    store_b, sync_b = _node(tmp_path, "team_b", "b")
    # 共享密钥对齐
    from coworker.team.sync import import_sync_secrets

    sync_b.secrets = import_sync_secrets(tmp_path / "b_secrets", sync_a.secrets.shared_key_b64)
    sync_a.record_peer_public_key(sync_b.secrets.public_key_hex)
    sync_b.record_peer_public_key(sync_a.secrets.public_key_hex)

    res = store_a.toggle_matrix_capability("worker", "issue_commands", True, by="a-me")
    # collect: 确定性 change_id — 跑两轮不重复生成
    c1 = [c for c in sync_a.collect_snapshot_changes() if c["entity_type"] == "matrix"]
    c2 = [c for c in sync_a.collect_snapshot_changes() if c["entity_type"] == "matrix"]
    assert len(c1) == 1 and c1[0]["change_id"] == f"matrix:worker:{res['ts']:.3f}"
    assert len(c2) == 0

    envs = sync_a.pack_for_transport(c1)
    merged = sync_b.merge_changes(sync_b.unpack_from_transport(envs))
    assert merged["applied"] == 1
    assert "issue_commands" in store_b.get_matrix_overrides()["worker"]["add"]
    register_matrix_provider(store_b.get_matrix_overrides)
    ok, _ = org_gate("worker", "run_shell", {"command": "ls"})
    assert ok is True  # B 端 gate 生效

    # B 端把能力回收后反向同步, A 收到后 gate 恢复拦截
    store_b.set_matrix_overrides("worker", add=[], remove=["issue_commands"], ts=res["ts"] + 5)
    changes = [c for c in sync_b.collect_snapshot_changes() if c["entity_type"] == "matrix"]
    assert len(changes) == 1
    sync_a.merge_changes(sync_a.unpack_from_transport(sync_b.pack_for_transport(changes)))
    register_matrix_provider(store_a.get_matrix_overrides)
    ok, _ = org_gate("worker", "run_shell", {"command": "ls"})
    assert ok is False


# -- 治理审计 -------------------------------------------------------------------


def test_audit_append_query_and_gate_sink(tmp_path):
    store = TeamStore(tmp_path / "team.db")
    store.append_audit("matrix.grant", actor="member-1", target="worker", detail={"capability": "vote"})
    store.append_audit("member.add", actor="member-1", target="member-2", detail={"name": "小王"})

    events = store.query_audit(limit=10)
    assert [e["action"] for e in events] == ["member.add", "matrix.grant"]  # 倒序
    assert events[0]["detail"]["name"] == "小王"
    only_matrix = store.query_audit(action="matrix.%")
    assert len(only_matrix) == 1
    page2 = store.query_audit(limit=1, before_seq=events[0]["seq"])
    assert page2 and page2[0]["action"] == "matrix.grant"

    # org gate 拦截 → sink 自动落审计 (manager 启动时注册的落点)
    denials: list = []
    register_audit_sink(lambda a, actor, target, detail: denials.append((a, target, detail)))
    ok, _ = org_gate("worker", "run_shell", {"command": "ls"})
    assert ok is False and len(denials) == 1
    assert denials[0][0] == "org_gate.deny" and denials[0][1] == "worker"
    assert denials[0][2]["tool"] == "run_shell"
