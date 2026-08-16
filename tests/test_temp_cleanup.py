"""S12 临时文件治理 (蜂群审计报告 G4) — 工作区临时产物自动清理.

契约:
  * 清理: 明显是中间产物的文件 (_part* / _chunk* / _fresh* / *.tmp /
    __pycache__ 等);
  * 保护: _swarm_reports/ (正式报告) / selfmade_tools/ (自造工具资产) /
    .qunwork/ (状态库) 永不删; coordination-report-*.md 等正式报告保留;
  * since: 只清理指定时间之后修改的临时文件 (run 期间产生);
  * dry_run: 只列出不删除。
"""

from __future__ import annotations

import time

from coworker.orchestrator.temp_cleanup import (
    cleanup_workspace_temp_files,
    collect_temp_files,
)


def _mk(ws, name, content="x"):
    p = ws / name
    p.write_text(content, encoding="utf-8")
    return p


def test_collects_obvious_temp_files(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk(ws, "_part01_x.md")
    _mk(ws, "_chunk.py")
    _mk(ws, "_fresh_test.txt")
    _mk(ws, "data.tmp")
    _mk(ws, "report.md")  # 正式文件
    temps = collect_temp_files(ws)
    names = {p.name for p in temps}
    assert "_part01_x.md" in names
    assert "_chunk.py" in names
    assert "_fresh_test.txt" in names
    assert "data.tmp" in names
    assert "report.md" not in names  # 正式文件不动


def test_protects_reports_and_assets(tmp_path):
    ws = tmp_path / "ws"
    (ws / "_swarm_reports").mkdir(parents=True)
    (ws / "selfmade_tools").mkdir(parents=True)
    (ws / ".qunwork").mkdir(parents=True)
    _mk(ws / "_swarm_reports", "20260816-report.md")
    _mk(ws / "selfmade_tools", "read_file_plain.py")
    _mk(ws / ".qunwork", "harness.db")
    _mk(ws, "_part01_x.md")  # 根目录临时文件
    temps = collect_temp_files(ws)
    names = {p.name for p in temps}
    assert "20260816-report.md" not in names  # 正式报告保护
    assert "read_file_plain.py" not in names  # 自造工具保护
    assert "harness.db" not in names  # 状态库保护
    assert "_part01_x.md" in names  # 临时文件清理


def test_keeps_coordination_and_health_reports(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk(ws, "coordination-report-orch_123.md")
    _mk(ws, "hive-health-report-1.md")
    _mk(ws, "roi-report-2026.html")
    _mk(ws, "_part_x.md")
    temps = collect_temp_files(ws)
    names = {p.name for p in temps}
    assert "coordination-report-orch_123.md" not in names
    assert "hive-health-report-1.md" not in names
    assert "roi-report-2026.html" not in names
    assert "_part_x.md" in names


def test_since_filter_only_run_period(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    old = _mk(ws, "_part_old.md")
    old_time = time.time() - 3600
    import os

    os.utime(old, (old_time, old_time))
    _mk(ws, "_part_new.md")
    since = time.time() - 60
    temps = collect_temp_files(ws, since=since)
    names = {p.name for p in temps}
    assert "_part_new.md" in names
    assert "_part_old.md" not in names  # run 前就有, 不清理


def test_cleanup_removes_and_dry_run_does_not(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk(ws, "_part01_x.md")
    _mk(ws, "_chunk.py")

    # dry_run: 不删除
    r = cleanup_workspace_temp_files(ws, dry_run=True)
    assert r["dry_run"] is True and r["count"] == 2
    assert (ws / "_part01_x.md").exists()

    # 实际清理
    r2 = cleanup_workspace_temp_files(ws)
    assert r2["count"] == 2
    assert not (ws / "_part01_x.md").exists()
    assert not (ws / "_chunk.py").exists()


def test_keep_exempts_explicit_output(tmp_path):
    """显式豁免: intent 命名的正式输出文件 (即使名字像临时) 不被清理 —
    防误删用户指定产物 (S12 修正: probe_test.md 不能因含 'test' 被删)。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    report = _mk(ws, "probe_test.md")
    _mk(ws, "_part01_x.md")
    temps = collect_temp_files(ws, keep=[report])
    names = {p.name for p in temps}
    assert "probe_test.md" not in names  # 豁免
    assert "_part01_x.md" in names  # 临时仍清理
