"""Skills-market + knowledge-library API endpoint tests (against the FastAPI app)."""

from __future__ import annotations

import base64
import io
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    from coworker.server.manager import SessionManager
    from coworker.server.app import create_app

    manager = SessionManager(
        workspace=str(tmp_path / "ws"),
        data_dir=str(tmp_path / "state"),
        model="test",
        provider=None,
    )
    app = create_app(manager)
    with TestClient(app) as c:
        yield c, manager


def _skill_zip_bytes(name: str = "cool-skill") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", f"---\nname: {name}\ndescription: a cool skill\nversion: 1.0.0\ncategory: dev\n---\n\nbody")
        zf.writestr("helper.py", "print('hi')")
    return buf.getvalue()


def test_skills_catalog_with_stats(client):
    c, _ = client
    r = c.get("/v1/skills")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()["skills"]]
    # 内置层(coworker/skills)的技能真实存在并进入 catalog
    assert "image-understanding" in names
    assert "code-bug-analysis" in names


def test_skill_import_export_roundtrip(client):
    c, _ = client
    r = c.post("/v1/skills/import", json={"zip_base64": base64.b64encode(_skill_zip_bytes()).decode()})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["name"] == "cool-skill"
    assert data["version"] == "1.0.0"

    # now in catalog with install count
    cats = c.get("/v1/skills").json()["skills"]
    assert cats[0]["name"] == "cool-skill"
    assert cats[0]["install_count"] == 1

    # detail
    det = c.get("/v1/skills/cool-skill").json()
    assert det["ok"] is True and det["category"] == "dev"

    # export → import into a second state via same manager loader
    ex = c.post("/v1/skills/export", json={"name": "cool-skill"}).json()
    assert ex["ok"] is True
    assert ex["zip_base64"]

    # rate
    rated = c.post("/v1/skills/cool-skill/rate", json={"score": 5}).json()
    assert rated["ok"] is True and rated["rating"] == 5.0

    # delete
    deleted = c.delete("/v1/skills/cool-skill").json()
    assert deleted["ok"] is True
    names = [s["name"] for s in c.get("/v1/skills").json()["skills"]]
    assert "cool-skill" not in names  # 导入的技能已删除(内置技能保留)


def test_skill_import_rejects_invalid_zip(client):
    c, _ = client
    r = c.post("/v1/skills/import", json={"zip_base64": base64.b64encode(b"not a zip").decode()})
    assert r.json()["ok"] is False


def test_skill_import_delete_refreshes_live_engine_loaders(client, tmp_path):
    """A running session's engine holds its own SkillLoader — importing/deleting a
    skill via the API must refresh it so load_skill resolves immediately."""
    from coworker.agent import build_engine
    from coworker.agents import cowork_agent
    from coworker.providers import ModelCapabilities

    class _Stub:
        def complete(self, **kwargs):  # pragma: no cover - not invoked
            raise NotImplementedError

        def capabilities(self, model):
            return ModelCapabilities()

    c, manager = client
    ws = tmp_path / "ws"
    ws.mkdir()
    engine = build_engine(agent=cowork_agent(), workspace=ws, provider=_Stub())
    try:
        manager._engines["fake-session"] = engine

        r = c.post(
            "/v1/skills/import",
            json={"zip_base64": base64.b64encode(_skill_zip_bytes()).decode()},
        )
        assert r.json()["ok"] is True
        assert engine.skill_loader.get("cool-skill") is not None

        assert c.delete("/v1/skills/cool-skill").json()["ok"] is True
        assert engine.skill_loader.get("cool-skill") is None
    finally:
        ex = getattr(engine, "executor", None)
        if ex is not None:
            ex.close()


def test_knowledge_add_list_search_delete(client, tmp_path):
    c, manager = client
    ws = str(tmp_path / "ws")
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    manager.resolve_workspace = lambda x=None: ws  # force workspace

    # manual entry
    r = c.post("/v1/knowledge", json={"title": "固态电池", "content": "固态电池以固态电解质替代液态电解液。"})
    assert r.json()["ok"] is True

    # workspace file scan
    (tmp_path / "ws" / "guide.md").write_text("QunWork 安装步骤：双击安装包完成。", encoding="utf-8")
    scan = c.post("/v1/knowledge/scan").json()
    assert scan["ok"] is True and scan["added"] >= 1

    items = c.get("/v1/knowledge").json()["items"]
    assert len(items) >= 2

    hits = c.get("/v1/knowledge/search", params={"q": "固态电池"}).json()
    assert hits["ok"] is True and hits["results"]

    # delete one
    item_id = items[0]["id"]
    assert c.delete(f"/v1/knowledge/{item_id}").json()["ok"] is True


def test_knowledge_import_folder_api(client, tmp_path):
    c, manager = client
    external = tmp_path / "docs"
    external.mkdir()
    (external / "readme.md").write_text("本地文件夹导入：知识文件库指南。", encoding="utf-8")

    r = c.post("/v1/knowledge/import-folder", json={"path": str(external)})
    data = r.json()
    assert data["ok"] is True and data["added"] >= 1

    # bad path -> ok false
    bad = c.post("/v1/knowledge/import-folder", json={"path": str(tmp_path / "nope")})
    assert bad.json()["ok"] is False

    # searchable
    hits = c.get("/v1/knowledge/search", params={"q": "知识文件库"}).json()
    assert hits["ok"] is True and hits["results"]


def test_knowledge_single_source_of_truth(client, tmp_path):
    """UI/API writes and the agent's knowledge_search must share ONE database —
    regression for the two-library split (state dir vs workspace .qunwork)."""
    from coworker.knowledge import resolve_knowledge_db_path
    from coworker.knowledge.store import KnowledgeStore

    c, manager = client
    # SessionManager's knowledge store lives at `base / knowledge.db`; the resolver
    # (what agent builds default to) must agree with it for the same data_dir.
    assert (manager._data_base / "knowledge.db") == resolve_knowledge_db_path(
        data_dir=manager._data_base
    )

    ws = str(tmp_path / "ws")
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    manager.resolve_workspace = lambda x=None: ws

    # an entry added via the UI/API ...
    r = c.post("/v1/knowledge", json={"title": "统一库", "content": "知识库单一路径验证内容。"})
    assert r.json()["ok"] is True

    # ... is visible to a store opened on the resolver path (what knowledge_search uses)
    s = KnowledgeStore(manager._data_base / "knowledge.db", workspace=ws)
    try:
        hits = s.search("单一路径", workspace=ws)
        assert hits and hits[0]["title"] == "统一库"
    finally:
        s.close()

    # empty add is rejected server-side
    bad = c.post("/v1/knowledge", json={"title": "", "content": "x"}).json()
    assert bad["ok"] is False


def test_orchestrate_api_executor_agent_validation(client):
    """POST /v1/orchestrate accepts executor_agent in ("cowork","code") and
    falls back to cowork for anything else (no 500)."""
    c, _ = client
    r = c.post("/v1/orchestrate", json={"intent": "x", "executor_agent": "hacker"})
    assert r.status_code == 200
    assert r.json()["ok"] is True  # invalid value fell back to cowork without crashing
    r2 = c.post("/v1/orchestrate", json={"intent": "x", "executor_agent": "code"})
    assert r2.status_code == 200 and r2.json()["ok"] is True
