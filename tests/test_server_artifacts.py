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

    # 6) M3: an absolute path OUTSIDE every artifact root (not under the
    # workspace, its parent, or the default workspace) is rejected — a cloned
    # repo's workspace must never read arbitrary machine files (secrets.json).
    import tempfile as _tf
    outside_dir = pathlib.Path(_tf.mkdtemp())  # sibling temp root, NOT under tmp_path
    outside = outside_dir / "secrets.json"
    outside.write_text("top-secret", encoding="utf-8")
    t, err = mgr._artifact_target("sid", str(outside))
    assert t is None and err == "not found"  # blocked by the roots gate


def test_artifact_read_blocks_absolute_path_outside_roots(tmp_path, monkeypatch):
    """End-to-end: /v1/artifacts/read with a path outside the session roots fails."""
    from coworker.server.manager import SessionManager

    ws = tmp_path / "ws"
    ws.mkdir()
    victim = None  # replaced below by a sibling temp dir outside the roots

    mgr = SessionManager.__new__(SessionManager)
    mgr.default_workspace = str(ws)

    class FakeSession:
        workspace = str(ws)
        extra_roots = []

    mgr.session_store = type("S", (), {"load": staticmethod(lambda sid: FakeSession())})()

    import tempfile as _tf
    victim_dir = pathlib.Path(_tf.mkdtemp())  # OUTSIDE the ws/parent/default roots
    victim = victim_dir / "secrets.json"
    victim.write_text("secret", encoding="utf-8")
    t, err = mgr._artifact_target("sid", str(victim))
    assert t is None and err == "not found"

    # a file INSIDE the workspace still reads fine
    good = ws / "ok.md"
    good.write_text("fine", encoding="utf-8")
    t, err = mgr._artifact_target("sid", str(good))
    assert err is None and t == good.resolve()


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


def test_swarm_template_track_record(tmp_path):
    """Strategy report 5.2.1: a template accumulates a track record (runs + successes)."""
    from coworker.conversations import ConversationStore

    store = ConversationStore(tmp_path / "conv.db")
    tmpl = store.add_swarm_template("周报", "每周市场分析", [{"id": "t0", "description": "收集", "deps": []}])
    assert tmpl["runs_count"] == 0 and tmpl["success_count"] == 0
    assert store.record_swarm_template_run(tmpl["id"], True) is True
    assert store.record_swarm_template_run(tmpl["id"], False) is True
    rows = store.list_swarm_templates()
    assert rows[0]["runs_count"] == 2 and rows[0]["success_count"] == 1


def test_team_memory_panel_crud(tmp_path):
    """5.2.2 team memory panel: list fields, search relevance, edit, delete."""
    from coworker.conversations import ConversationStore
    from coworker.server.manager import SessionManager

    store = ConversationStore(tmp_path / "conv.db")
    mgr = SessionManager.__new__(SessionManager)
    mgr.session_store = store
    from coworker.memory import Scope, SQLiteMemoryStore

    mgr.memory_store = SQLiteMemoryStore(tmp_path / "mem.db")
    a = mgr.add_memory("项目代号凤凰,Q3 上线", scope="workspace", workspace=str(tmp_path))
    b = mgr.add_memory("每周五下午开例会", scope="workspace", workspace=str(tmp_path))
    # list fields
    rows = mgr.list_memory()
    assert len(rows) == 2 and "workspace" in rows[0] and "created_at" in rows[0]
    # search relevance
    hits = mgr.search_memory("凤凰", k=5)
    assert hits and hits[0]["id"] == a["id"]
    assert all("content" in h for h in hits)
    # edit
    upd = mgr.update_memory(a["id"], "项目代号凤凰,Q3 上线,负责人小李")
    assert upd and upd["content"].endswith("负责人小李")
    assert mgr.update_memory(9999, "x") is None  # missing id
    # clear
    assert mgr.delete_memory(b["id"]) is True
    assert mgr.delete_memory(9999) is False
    assert len(mgr.list_memory()) == 1


def test_automation_result_sinks_into_knowledge(tmp_path):
    """Asset loop A1: a completed automation run auto-indexes into the unified
    knowledge library (kind=automation) — retrievable like any knowledge."""
    from coworker.conversations import ConversationStore
    from coworker.knowledge import KnowledgeStore
    from coworker.server.manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr.session_store = ConversationStore(tmp_path / "conv.db")
    mgr.knowledge = KnowledgeStore(tmp_path / "knowledge.db", workspace=str(tmp_path))
    mgr.memory_store = None

    # simulate _run_scheduled_task's ingestion block
    class T:
        title = "周报"
        run_count = 3
        workspace = str(tmp_path)

    mgr.knowledge.add_text(
        title=f"{T.title} · run #{T.run_count}",
        content="本周完成 66 次提交,蜂群指挥台落地…" + "内容" * 50,
        kind="automation",
        workspace=T.workspace,
    )
    hits = mgr.knowledge.search("蜂群指挥台", k=5, workspace=str(tmp_path))
    assert hits and hits[0]["kind"] == "automation"
    items = mgr.knowledge.list_items(workspace=str(tmp_path))
    assert any(i["kind"] == "automation" for i in items)


def test_swarm_report_sinks_into_knowledge_and_tallies_template(tmp_path):
    """Asset loop A2: completed swarm report lands in knowledge (kind=swarm_report)
    and the originating template's track record is auto-incremented."""
    from coworker.conversations import ConversationStore
    from coworker.knowledge import KnowledgeStore
    from coworker.server.manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr.session_store = ConversationStore(tmp_path / "conv.db")
    mgr.knowledge = KnowledgeStore(tmp_path / "knowledge.db", workspace=str(tmp_path))
    mgr.memory_store = None
    tmpl = mgr.session_store.add_swarm_template("周报", "生成周报", [{"id": "t0", "description": "写", "deps": []}])
    assert mgr.session_store.record_swarm_template_run(tmpl["id"], True)
    rows = mgr.session_store.list_swarm_templates()
    assert rows[0]["runs_count"] == 1 and rows[0]["success_count"] == 1

    mgr.knowledge.add_text(
        title="蜂群报告 abc12345",
        content="本周工作与成果:63 次提交…" + "内容" * 50,
        kind="swarm_report",
        workspace=str(tmp_path),
    )
    hits = mgr.knowledge.search("本周工作与成果 提交", k=5, workspace=str(tmp_path))
    assert hits and hits[0]["kind"] == "swarm_report"


def test_unified_asset_search(tmp_path):
    """Asset loop Phase 2: one query hits knowledge / skills / templates /
    memories / swarm runs together."""
    from coworker.conversations import ConversationStore
    from coworker.knowledge import KnowledgeStore
    from coworker.memory import SQLiteMemoryStore
    from coworker.orchestrator.run_store import OrchestrationRunStore
    from coworker.server.manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr.session_store = ConversationStore(tmp_path / "conv.db")
    mgr.knowledge = KnowledgeStore(tmp_path / "knowledge.db", workspace=str(tmp_path))
    mgr.memory_store = SQLiteMemoryStore(tmp_path / "mem.db")
    mgr.orchestration_store = OrchestrationRunStore(tmp_path / "orch.db")
    mgr.default_workspace = str(tmp_path)
    mgr.skill_market = None

    class FakeSkills:
        def catalog(self):
            return [
                {"name": "周报生成", "description": "用蜂群自动生成周报", "body": "步骤…"},
                {"name": "code-review", "description": "审查代码", "body": "…"},
            ]

    mgr.skill_loader = FakeSkills()
    mgr.skill_market = type("M", (), {"all_stats": lambda self: {}})()
    mgr.list_skills = lambda: [
        {**r, "install_count": 0, "rating": None, "rating_count": 0}
        for r in mgr.skill_loader.catalog()
    ]

    # seed one of each asset
    mgr.knowledge.add_text("周报知识", "本周工作与成果 提交", kind="swarm_report", source_run_id="orch_abc")
    mgr.session_store.add_swarm_template("周报模板", "生成周报", [{"id": "t0", "description": "写", "deps": []}])
    mgr.memory_store.add("周报上周总结", scope="workspace")
    mgr.orchestration_store.create_run("用蜂群生成本周总结并输出周报")
    hits = mgr.search_assets("本周工作与成果 提交 周报", k=5)
    assert hits["knowledge"] and hits["knowledge"][0]["source_run_id"] == "orch_abc"
    assert hits["skills"], "skill must be searchable by name/description"
    assert hits["templates"] and hits["templates"][0]["title"] == "周报模板"
    assert hits["memories"]
    assert hits["runs"]
    # source_run_id surfaced through list_items too
    items = mgr.knowledge.list_items(workspace=str(tmp_path))
    assert any(i.get("source_run_id") == "orch_abc" for i in items)


def test_asset_lifecycle_retire_and_use_count(tmp_path):
    """Phase 3: retired entries vanish from search but stay for audit; every
    retrieval ticks use_count."""
    from coworker.knowledge import KnowledgeStore

    ks = KnowledgeStore(tmp_path / "knowledge.db", workspace=str(tmp_path))
    item = ks.add_text("过时报告", "旧版本内容" + "内容" * 30, kind="automation")
    hits = ks.search("旧版本内容", k=5, workspace=str(tmp_path))
    assert hits and hits[0]["item_id"] == item
    # retrieval ticked the counter
    listed = ks.list_items(workspace=str(tmp_path))
    assert listed[0]["use_count"] >= 1
    # retire → hidden from search/list
    assert ks.set_retired(item, True)
    assert ks.search("旧版本内容", k=5, workspace=str(tmp_path)) == []
    assert ks.list_items(workspace=str(tmp_path)) == []
    # restore
    assert ks.set_retired(item, False)
    assert ks.search("旧版本内容", k=5, workspace=str(tmp_path))


def test_calendar_phase_clusters_by_rhythm():
    """Phase 3: a weekly task lands in the same phase every week (ISO week),
    a daily task in the same day-phase — not run-ordinal."""
    import datetime
    from coworker.automation.models import Schedule, ScheduledTask
    from coworker.server.manager import _calendar_phase

    weekly = ScheduledTask(id="w", title="周报", instructions="", schedule=Schedule(kind="cron", cron="0 9 * * 1"), workspace=".")
    daily = ScheduledTask(id="d", title="日志", instructions="", schedule=Schedule(kind="cron", cron="0 9 * * *"), workspace=".")
    now = datetime.date.today()
    assert _calendar_phase(weekly, fallback=5) == now.isocalendar()[1] % 60
    assert _calendar_phase(daily, fallback=5) == now.timetuple().tm_yday % 60
    # non-cron falls back to run ordinal
    assert _calendar_phase(ScheduledTask(id="x", title="x", instructions="", schedule=Schedule(kind="once", fire_at="2026-08-06T10:00"), workspace="."), fallback=7) == 7


def test_benchmark_templates_idempotent_seed(tmp_path):
    """Health-check template seeds even when other templates already exist."""
    from coworker.conversations import ConversationStore
    from coworker.server.manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr.session_store = ConversationStore(tmp_path / "conv.db")
    mgr.session_store.add_swarm_template("已有模板", "旧意图", [])
    mgr._seed_benchmark_templates()
    titles = [t["title"] for t in mgr.session_store.list_swarm_templates()]
    assert "组织资产健康体检 · 协同样板" in titles
    assert "每周自动化周报 · 协同样板" in titles
    # idempotent: running again adds nothing
    n = len(mgr.session_store.list_swarm_templates())
    mgr._seed_benchmark_templates()
    assert len(mgr.session_store.list_swarm_templates()) == n
