"""Agents (Code/Chat) + SKILL.md loader (catalog + load_skill)."""

from __future__ import annotations

from coworker.agent import build_engine
from coworker.agents import AgentContext, chat_agent, code_agent, get_agent
from coworker.providers import ModelCapabilities
from coworker.skills import SkillLoader, skill_catalog_text, skill_tools
from coworker.tools import ToolRegistry
from coworker.tools.shell import LocalExecutor
from coworker.tools.todo import TodoList


class _Stub:
    def complete(self, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def capabilities(self, model):
        return ModelCapabilities()


# -- agents ---------------------------------------------------------------------


def test_code_agent_tools(tmp_path):
    ex = LocalExecutor(cwd=tmp_path, default_timeout=5)
    try:
        ctx = AgentContext(workspace=tmp_path, executor=ex, todo=TodoList())
        names = {getattr(t, "__name__", "?") for t in code_agent().build_tools(ctx)}
        assert {
            "read_file",
            "write_file",
            "git_status",
            "run_shell",
            "todo_write",
        } <= names
    finally:
        ex.close()


def test_chat_agent_has_no_workspace_tools():
    assert chat_agent().build_tools(AgentContext()) == []
    assert chat_agent().needs_workspace is False
    assert code_agent().needs_workspace is True


def test_get_agent_fallback():
    assert get_agent("chat").name == "chat"
    # Unknown ids fall back to the default persona (Cowork), per the persona registry.
    assert get_agent("nope").name == "cowork"


# -- SKILL.md loader ------------------------------------------------------------


def _make_skill(skills_dir, name, desc, body):
    d = skills_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n{body}", encoding="utf-8"
    )


def test_skill_loader_catalog_and_load(tmp_path):
    skills_dir = tmp_path / "skills"
    _make_skill(
        skills_dir, "pdf", "extract text from PDFs", "Use pdfplumber to extract text."
    )
    loader = SkillLoader([skills_dir])

    row = loader.catalog()[0]
    assert row["name"] == "pdf"
    assert row["description"] == "extract text from PDFs"
    assert row["version"] == "0.1.0"
    assert row["category"] == "general"
    assert "pdf: extract text from PDFs" in skill_catalog_text(loader)

    reg = ToolRegistry()
    reg.register_all(skill_tools(loader))
    loaded = reg.execute("load_skill", {"name": "pdf"})
    assert "pdfplumber" in loaded["instructions"]
    assert reg.execute("load_skill", {"name": "missing"})["error"]


# -- P1-8 转正链: 涌现草稿 (draft) → 正式技能 -------------------------------------


def test_draft_skill_roundtrip_and_promote(tmp_path):
    """save_skill(draft=True) 生成草稿; update_skill(draft=False) 转正 —
    draft 状态经 frontmatter 写入/解析往返保持一致, 其他字段不被破坏。"""
    import re as _re

    skills_dir = tmp_path / "skills"
    loader = SkillLoader([skills_dir])
    loader.save_skill(
        name="emergence-守恒分流",
        description="[涌现-共现模式压缩] 守恒⊕分流",
        body="# 草稿正文",
        version="0.0.1",
        category="emergence",
        tags=["hornet", "emergence", "hypernode", "draft"],
        draft=True,
        source="hornet_emergence",
    )
    row = next(r for r in loader.catalog() if r["name"] == "emergence-守恒分流")
    assert row["draft"] is True
    assert row["source"] == "hornet_emergence"
    assert "draft" in row["tags"]

    # manager.skill_promote 的清洗语义 (同一 regex): description 去 [涌现-…] 前缀
    new_desc = _re.sub(r"^\[涌现-[^\]]+\]\s*", "", row["description"])
    assert new_desc == "守恒⊕分流"

    loader.update_skill(
        "emergence-守恒分流",
        description=new_desc,
        tags=[t for t in row["tags"] if t != "draft"],
        draft=False,
        version="0.1.0",
    )
    published = next(r for r in loader.catalog() if r["name"] == "emergence-守恒分流")
    assert published["draft"] is False
    assert "draft" not in published["tags"]
    assert published["description"] == "守恒⊕分流"
    assert published["version"] == "0.1.0"
    assert published["category"] == "emergence"  # 未指定的字段保持原值

    # 转正后正文完好 (update_skill 只动 frontmatter)
    skill = loader.get("emergence-守恒分流")
    assert skill is not None
    md = (skills_dir / "emergence-守恒分流" / "SKILL.md").read_text(encoding="utf-8")
    assert "草稿正文" in md
    assert "draft: false" in md


# -- engine assembly per agent --------------------------------------------------


def test_build_engine_chat(tmp_path):
    engine = build_engine(agent=chat_agent(), provider=_Stub())
    assert "load_skill" in engine.registry.names()
    assert "read_file" not in engine.registry.names()
    assert engine.executor is None
    assert engine.agent_name == "chat"


def test_build_engine_code_has_agents_md_and_skills(tmp_path):
    (tmp_path / "AGENTS.md").write_text("PROJECT RULE: prefer pathlib.")
    engine = build_engine(agent=code_agent(), workspace=tmp_path, provider=_Stub())
    try:
        assert "prefer pathlib" in engine.messages[0]["content"]
        assert "todo_write" in engine.registry.names()
        assert "load_skill" in engine.registry.names()
        assert engine.agent_name == "code"
    finally:
        engine.executor.close()
