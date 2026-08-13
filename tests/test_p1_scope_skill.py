"""P1-5/P1-6/P1-8 后端单元测试: scope 检查, skill.lock, 安全评分, 涌现转技能."""
import json
import tempfile
from pathlib import Path

import pytest


# -- P1-5 零信任能力袋: connector_scopes 测试 --------------------------------

def test_scope_level():
    from coworker.connector_scopes import scope_level
    assert scope_level("read:repo") == 1
    assert scope_level("write:pr") == 2
    assert scope_level("admin:repo") == 3
    assert scope_level("unknown:foo") == 0


def test_scope_implies():
    from coworker.connector_scopes import scope_implies
    # 同 resource 高级隐含低级
    assert scope_implies("write:repo", "read:repo") is True
    assert scope_implies("admin:repo", "write:repo") is True
    assert scope_implies("admin:repo", "read:repo") is True
    # 不同 resource 不隐含
    assert scope_implies("write:repo", "read:channel") is False
    # 低级不隐含高级
    assert scope_implies("read:repo", "write:repo") is False


def test_get_tool_scopes():
    from coworker.connector_scopes import get_tool_scopes, parse_connector_from_tool
    # GitHub PR 创建需要 write:pr
    scopes = get_tool_scopes("github__create_pr")
    assert "write:pr" in scopes
    # 内置工具无 scope
    assert get_tool_scopes("load_skill") == []
    # MCP 工具
    scopes = get_tool_scopes("mcp__myserver__some_tool")
    # 未声明的 MCP 工具默认 write:default → 零信任 fail-closed (需审批)
    assert scopes == ["write:default"]
    # 未声明的连接器工具同样 fail-closed
    assert get_tool_scopes("newconn__do_thing") == ["write:default"]
    # connector 解析
    assert parse_connector_from_tool("github__create_pr") == "github"
    assert parse_connector_from_tool("mcp__myserver__tool") == "myserver"
    assert parse_connector_from_tool("load_skill") is None


def test_persona_scope_store():
    from coworker.connector_scopes import PersonaScopeStore
    with tempfile.TemporaryDirectory() as d:
        store = PersonaScopeStore(Path(d) / "scopes.db")
        try:
            # 初始为空
            assert store.get_scopes("cowork", "github") == []
            # 设置 scope
            store.set_scopes("cowork", "github", ["read:repo", "write:pr"])
            assert store.get_scopes("cowork", "github") == ["read:repo", "write:pr"]
            # 全量读取
            all_scopes = store.get_all_scopes("cowork")
            assert "github" in all_scopes
            # 删除
            store.remove_scopes("cowork", "github")
            assert store.get_scopes("cowork", "github") == []
        finally:
            store.close()


def test_check_scope_allowed():
    from coworker.connector_scopes import PersonaScopeStore, check_scope
    with tempfile.TemporaryDirectory() as d:
        store = PersonaScopeStore(Path(d) / "scopes.db")
        try:
            store.set_scopes("cowork", "github", ["write:repo"])
            # write:repo 隐含 read:repo, 所以 list_repos (read:repo) 应该放行
            dec = check_scope("cowork", "github__list_repos", store)
            assert dec.allowed is True
            # create_pr 需要 write:pr, 只授了 write:repo → 越权
            dec = check_scope("cowork", "github__create_pr", store)
            assert dec.allowed is False
            assert dec.needs_approval is True
        finally:
            store.close()


def test_check_scope_default_read():
    from coworker.connector_scopes import PersonaScopeStore, check_scope
    with tempfile.TemporaryDirectory() as d:
        store = PersonaScopeStore(Path(d) / "scopes.db")
        try:
            # 未配置 scope → 默认只允许 read 级别
            dec = check_scope("cowork", "github__list_repos", store)
            assert dec.allowed is True
            # write 级别需要审批
            dec = check_scope("cowork", "github__create_pr", store)
            assert dec.allowed is False
            assert dec.needs_approval is True
        finally:
            store.close()


def test_national_connectors_registered():
    from coworker.connectors.descriptors import DESCRIPTORS
    names = {d.name for d in DESCRIPTORS}
    assert "wecom" in names
    assert "dingtalk" in names
    assert "feishu" in names


def test_national_connector_senders():
    from coworker.connectors.senders import DEFAULT_SENDERS
    assert "wecom" in DEFAULT_SENDERS
    assert "dingtalk" in DEFAULT_SENDERS
    assert "feishu" in DEFAULT_SENDERS


# -- P1-6 Skill.lock 测试 ----------------------------------------------------

def test_skill_lock_generate_and_verify():
    from coworker.skills.lock import generate_lock, verify_lock, save_lock, load_lock
    tools = [
        {"name": "github__create_pr", "params": {"properties": {"title": {}, "body": {}}}},
        {"name": "slack__post_message", "params": {"properties": {"channel": {}, "text": {}}}},
    ]
    lock = generate_lock("my-skill", "0.1.0", tools)
    assert lock["skill_name"] == "my-skill"
    assert len(lock["tools"]) == 2
    assert lock["tools"][0]["name"] == "github__create_pr"
    assert "schema_hash" in lock["tools"][0]
    assert "title" in lock["tools"][0]["params"]

    # 同样的 tools → 兼容
    report = verify_lock(lock, tools)
    assert report["compatible"] is True

    # 缺一个工具 → 不兼容
    report = verify_lock(lock, [tools[0]])
    assert report["compatible"] is False
    assert "slack__post_message" in report["missing_tools"]

    # schema 变化 → 不兼容
    changed_tools = [
        {"name": "github__create_pr", "params": {"properties": {"title": {}, "body": {}, "head": {}}}},
        {"name": "slack__post_message", "params": {"properties": {"channel": {}, "text": {}}}},
    ]
    report = verify_lock(lock, changed_tools)
    assert report["compatible"] is False
    assert len(report["changed_tools"]) == 1


def test_skill_lock_save_load():
    from coworker.skills.lock import generate_lock, save_lock, load_lock
    with tempfile.TemporaryDirectory() as d:
        lock = generate_lock("test", "0.1.0", [{"name": "tool1", "params": {}}])
        path = save_lock(d, lock)
        assert path.exists()
        loaded = load_lock(d)
        assert loaded["skill_name"] == "test"
        assert loaded["tools"][0]["name"] == "tool1"


# -- P1-6 Skill 安全评分测试 -------------------------------------------------

def test_security_score_low_risk():
    from coworker.skills.security import analyze_skill_content
    # 纯文本, 无风险操作
    report = analyze_skill_content("# My Skill\n\nThis is a safe skill that does nothing.")
    assert report["score"] == 0
    assert report["level"] == "low"


def test_security_score_high_risk():
    from coworker.skills.security import analyze_skill_content
    # 含 eval + subprocess + requests
    content = """
import subprocess
import requests
result = eval("1+1")
subprocess.run(["ls"])
requests.get("https://example.com")
"""
    report = analyze_skill_content(content)
    assert report["score"] > 50
    assert report["level"] in ("high", "critical")
    # 应检测到 3 类风险
    labels = [f["label"] for f in report["findings"]]
    assert any("eval" in l for l in labels)
    assert any("subprocess" in l for l in labels)
    assert any("requests" in l for l in labels)


# -- P1-6 Skill 兼容性测试 ---------------------------------------------------

def test_compatibility_no_lock():
    from coworker.skills.compatibility import check_skill_compatibility
    with tempfile.TemporaryDirectory() as d:
        report = check_skill_compatibility(d, [])
        assert report["has_lock"] is False
        assert report["compatible"] is True  # 无 lock 不阻断


def test_compatibility_with_lock():
    from coworker.skills.lock import generate_lock, save_lock
    from coworker.skills.compatibility import check_skill_compatibility
    with tempfile.TemporaryDirectory() as d:
        lock = generate_lock("test", "0.1.0", [
            {"name": "github__list_repos", "params": {"properties": {"org": {}}}}
        ])
        save_lock(d, lock)
        # 兼容的工具
        report = check_skill_compatibility(d, [
            {"name": "github__list_repos", "params": {"properties": {"org": {}}}}
        ])
        assert report["compatible"] is True
        # 不兼容 (参数变了)
        report = check_skill_compatibility(d, [
            {"name": "github__list_repos", "params": {"properties": {"org": {}, "user": {}}}}
        ])
        assert report["compatible"] is False


# -- P1-6 Skill draft 字段测试 -----------------------------------------------

def test_skill_draft_field():
    from coworker.skills.base import SkillLoader
    with tempfile.TemporaryDirectory() as d:
        loader = SkillLoader([d])
        loader.save_skill(
            name="test-draft",
            description="A draft skill",
            body="# Draft\n\nTBD",
            draft=True,
            source="hornet_emergence",
        )
        skill = loader.get("test-draft")
        assert skill is not None
        assert skill.draft is True
        assert skill.source == "hornet_emergence"


# -- P1-8 涌现转技能测试 -----------------------------------------------------

def test_emergence_to_skill_generation():
    """测试涌现条目能正确生成 Draft Skill."""
    from coworker.skills.base import SkillLoader
    with tempfile.TemporaryDirectory() as d:
        loader = SkillLoader([d])

        # 模拟 _hornet_act_to_skill 的核心逻辑
        title = "测试知识 ⊕ 另一个知识"
        detail = {"members": [1, 2], "co_occurrences": 5, "summary": "测试摘要"}
        kind = "hypernode"

        import re
        raw_name = title.replace("⊕", "-").replace("·", "-").replace(":", "-")
        raw_name = re.sub(r"[^\w\u4e00-\u9fff\-]", "-", raw_name).strip("-")
        skill_name = f"emergence-{raw_name}"[:60].lower()

        loader.save_skill(
            name=skill_name,
            description=f"[涌现-共现模式压缩] {title[:60]}",
            body=f"# {title}\n\n涌现详情: {detail}",
            version="0.0.1",
            category="emergence",
            tags=["hornet", "emergence", "draft"],
            draft=True,
            source="hornet_emergence",
        )

        skill = loader.get(skill_name)
        assert skill is not None
        assert skill.draft is True
        assert skill.source == "hornet_emergence"
        assert skill.version == "0.0.1"
        assert "emergence" in skill.tags


# -- security-review 修复回归 (MEDIUM-1 脱敏 / MEDIUM-2 scope fail-closed) ----
import os

def test_decision_trace_redacts_sensitive_args(tmp_path):
    """工具参数中的密钥/内容不得明文进入决策轨迹(内存 + audit 镜像)。"""
    from coworker.engine import TurnEngine
    from coworker.providers import AssistantTurn

    class _P:
        def complete(self, **kw):
            return AssistantTurn(text="ok")
        def capabilities(self, model):
            return None

    engine = TurnEngine(
        provider=_P(), registry=None, permissions=None,  # type: ignore[arg-type]
        model="m",
    )
    engine._record_decision(
        "tool_selection",
        tool="write_file",
        arguments={"path": "secret.txt", "api_key": "sk-abc123", "content": "x" * 500},
        candidates=[{"name": "write_file", "arguments": {"password": "hunter2"}}],
    )
    entry = engine.decision_trace[-1]
    assert entry["arguments"]["api_key"] == "***"
    assert entry["arguments"]["content"] != "x" * 500  # 截断
    assert "hunter2" not in str(entry)  # 嵌套 candidates 也脱敏
    assert "sk-abc123" not in str(entry)


def test_scope_undeclared_tool_is_fail_closed(tmp_path):
    """MCP/未声明连接器工具默认 write 级 → 未配置 persona 升级审批(零信任)。"""
    from coworker.connector_scopes import PersonaScopeStore, check_scope

    store = PersonaScopeStore(tmp_path / "scopes.db")
    dec = check_scope("gm", "mcp__myserver__tool", store)
    assert dec.allowed is False and dec.needs_approval is True
    dec2 = check_scope("gm", "newconn__do_thing", store)
    assert dec2.allowed is False and dec2.needs_approval is True
    # 内置工具仍无 scope 约束
    assert check_scope("gm", "load_skill", store).allowed is True
