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
    assert r.json()["skills"] == []


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
    assert c.get("/v1/skills").json()["skills"] == []


def test_skill_import_rejects_invalid_zip(client):
    c, _ = client
    r = c.post("/v1/skills/import", json={"zip_base64": base64.b64encode(b"not a zip").decode()})
    assert r.json()["ok"] is False


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
