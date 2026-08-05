"""Artifact path resolution: session-workspace relative, absolute cross-workspace,
and URL-encoded (Chinese) filenames must all open."""

import pathlib
import sys
from pathlib import Path
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def test_artifact_target_resolution(tmp_path, monkeypatch):
    from coworker.server.manager import SessionManager

    session_ws = tmp_path / "session"
    primary_ws = tmp_path / "primary"
    session_ws.mkdir()
    primary_ws.mkdir()

    rel_file = session_ws / "note.md"
    rel_file.write_text("relative", encoding="utf-8")
    abs_file = primary_ws / "突围策略报告.md"  # Chinese name, lives OUTSIDE the session ws
    abs_file.write_text("报告正文", encoding="utf-8")

    mgr = SessionManager.__new__(SessionManager)
    mgr.default_workspace = str(primary_ws)

    class FakeSession:
        workspace = str(session_ws)

    mgr.session_store = type("S", (), {"load": staticmethod(lambda sid: FakeSession())})()

    # 1) relative to session workspace
    t, err = mgr._artifact_target("sid", "note.md")
    assert err is None and t == rel_file.resolve()

    # 2) absolute path in the primary workspace (the "not found" scenario)
    t, err = mgr._artifact_target("sid", str(abs_file))
    assert err is None and t == abs_file.resolve()
    assert t.read_text(encoding="utf-8") == "报告正文"

    # 3) URL-encoded Chinese filename, absolute
    import urllib.parse
    encoded = urllib.parse.quote(str(abs_file))
    assert "%" in encoded
    t, err = mgr._artifact_target("sid", encoded)
    assert err is None and t == abs_file.resolve()

    # 4) relative name found via primary-workspace fallback
    t, err = mgr._artifact_target("sid", "突围策略报告.md")
    assert err is None and t == abs_file.resolve()

    # 5) missing file → not found
    t, err = mgr._artifact_target("sid", "nope.md")
    assert t is None and err == "not found"


def test_list_artifacts_skips_deps_and_merges_primary(tmp_path, monkeypatch):
    from coworker.server.manager import SessionManager

    session_ws = tmp_path / "session"
    primary_ws = tmp_path / "primary"
    session_ws.mkdir()
    primary_ws.mkdir()
    (session_ws / "report.md").write_text("deliverable", encoding="utf-8")
    (session_ws / "node_modules" / "x" / "noise.js").parent.mkdir(parents=True)
    (session_ws / "node_modules" / "x" / "noise.js").write_text("noise", encoding="utf-8")
    (session_ws / "_internal" / "y.py").parent.mkdir(parents=True)
    (session_ws / "_internal" / "y.py").write_text("noise", encoding="utf-8")
    (primary_ws / "primary-report.md").write_text("from primary", encoding="utf-8")

    mgr = SessionManager.__new__(SessionManager)
    mgr.default_workspace = str(primary_ws)
    mgr.session_store = type("S", (), {"load": staticmethod(lambda sid: type("R", (), {"workspace": str(session_ws)})())})()

    arts = mgr.list_artifacts("sid")
    paths = [a["path"] for a in arts]
    assert "report.md" in paths                     # session deliverable listed
    assert any("primary-report.md" in p for p in paths)  # primary merged (rel differs by root order)
    assert not any("node_modules" in p for p in paths)
    assert not any("_internal" in p for p in paths)


def test_list_artifacts_includes_session_parent(tmp_path):
    """The session workspace's parent directory is merged too — that's where the
    user's primary workspace (reports, research folders) usually lives."""
    from coworker.server.manager import SessionManager

    ws = tmp_path / "proj" / "session"  # session workspace nested under tmp_path/proj
    ws.mkdir(parents=True)
    (ws / "note.md").write_text("n", encoding="utf-8")
    (tmp_path / "proj" / "parent-report.md").write_text("from parent", encoding="utf-8")

    mgr = SessionManager.__new__(SessionManager)
    mgr.default_workspace = None
    mgr.session_store = type("S", (), {"load": staticmethod(lambda sid: type("R", (), {"workspace": str(ws)})())})()

    arts = mgr.list_artifacts("sid")
    paths = [a["path"] for a in arts]
    assert "note.md" in paths
    assert "parent-report.md" in paths  # sibling of the session workspace = its parent


def test_read_artifact_resolves_parent_workspace_path(tmp_path):
    """Relative paths that came from the merged parent-workspace scan must open too."""
    from coworker.server.manager import SessionManager

    ws = tmp_path / "proj" / "session"
    ws.mkdir(parents=True)
    parent_file = tmp_path / "proj" / "离散周期神经网络DPNN" / "OIR 框架.md"
    parent_file.parent.mkdir()
    parent_file.write_text("研究文档", encoding="utf-8")

    mgr = SessionManager.__new__(SessionManager)
    mgr.default_workspace = None
    mgr.session_store = type("S", (), {"load": staticmethod(lambda sid: type("R", (), {"workspace": str(ws)})())})()

    t, err = mgr._artifact_target("sid", "离散周期神经网络DPNN/OIR 框架.md")
    assert err is None and t == parent_file.resolve()
    assert t.read_text(encoding="utf-8") == "研究文档"


def test_team_package_export_import_roundtrip(tmp_path, monkeypatch):
    """Team workspace: export bundles templates+knowledge+skills; import restores
    them on a fresh store (dedup by title)."""
    import zipfile
    from coworker.conversations import ConversationStore
    from coworker.server.manager import SessionManager

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir(); dst.mkdir()
    (src / "skills" / "demo").mkdir(parents=True)
    (src / "skills" / "demo" / "SKILL.md").write_text("# demo\ninstructions", encoding="utf-8")
    (src / "skills" / "demo" / "helper.py").write_text("x=1", encoding="utf-8")

    def _mk(path: Path, seed: bool) -> SessionManager:
        mgr = SessionManager.__new__(SessionManager)
        mgr.default_workspace = str(path)
        mgr.session_store = ConversationStore(path / "data")
        mgr.skill_loader = type("L", (), {"catalog": staticmethod(lambda: []), "get": staticmethod(lambda n: None), "refresh": staticmethod(lambda: None)})()
        if seed:
            mgr.session_store.add_swarm_template("市场模板", "生成市场报告", [])
            mgr.session_store.add_task_template("周报", "生成周报")
        mgr.knowledge = type("K", (), {"list_items": staticmethod(lambda *a, **kw: [{"title": "研究笔记", "content": "正文"}]), "count_items": staticmethod(lambda *a, **kw: 1)})()
        mgr.knowledge_add = staticmethod(lambda t, c, workspace=None: None)
        return mgr

    exporter = _mk(src, seed=True)
    # stub skill loader with real dir for export
    exporter.skill_loader = type("L", (), {
        "catalog": staticmethod(lambda: [{"name": "demo"}]),
        "get": staticmethod(lambda n: type("S", (), {"path": str(src / "skills" / "demo")})()),
    })()
    res = exporter.export_team_package()
    assert res["ok"] is True
    zpath = Path(res["path"])
    assert zpath.exists()
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        assert "swarm-templates.json" in names and "task-templates.json" in names
        assert any(n.startswith("skills/demo/") for n in names)

    # import into a fresh instance (empty stores)
    importer = _mk(dst, seed=False)
    importer.skill_loader = type("L", (), {"catalog": staticmethod(lambda: []), "get": staticmethod(lambda n: None), "refresh": staticmethod(lambda: None)})()
    r2 = importer.import_team_package(str(zpath))
    assert r2["ok"] is True
    assert r2["imported"]["swarm_templates"] == 1
    assert r2["imported"]["task_templates"] == 1
    assert r2["imported"]["skills"] == 1
    rows = importer.session_store.list_swarm_templates()
    assert any("市场模板" in t["title"] for t in rows)

    # idempotent: second import adds nothing (dedup by title)
    r3 = importer.import_team_package(str(zpath))
    assert r3["imported"]["swarm_templates"] == 0
