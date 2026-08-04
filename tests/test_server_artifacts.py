"""Artifact path resolution: session-workspace relative, absolute cross-workspace,
and URL-encoded (Chinese) filenames must all open."""

import pathlib
import sys
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
    assert "primary-report.md" in paths             # primary workspace merged
    assert not any("node_modules" in p for p in paths)
    assert not any("_internal" in p for p in paths)
