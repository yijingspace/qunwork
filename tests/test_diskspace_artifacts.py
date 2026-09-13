"""Disk guard + artifact verification + non-silent storage failures.

Regression origin (owner-hit 2026-09-13): a fully-allocated volume made a swarm run
write a 0-byte deliverable and then freeze as a permanently-"running" ghost, because
(a) nothing checked that the workspace was writable, (b) `artifact:` claims were taken
on faith, and (c) a failing event store was logged and forgotten.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coworker.diskspace import check_writable, free_bytes, human_size
from coworker.orchestrator.artifacts import (
    artifact_refs,
    artifact_warning,
    verify_artifacts,
)
from coworker.providers import ModelCapabilities, ProviderClient

# -- disk guard ---------------------------------------------------------------------------


def test_writable_directory_passes(tmp_path):
    report = check_writable(tmp_path)
    assert report.ok is True
    assert report.reason == "ok"
    # the probe must clean up after itself
    assert list(tmp_path.glob(".qunwork-writeprobe-*")) == []


def test_creates_missing_directory(tmp_path):
    target = tmp_path / "nested" / "workspace"
    report = check_writable(target)
    assert report.ok is True
    assert target.is_dir()


def test_low_free_space_is_refused_with_the_volume_named(tmp_path, monkeypatch):
    import coworker.diskspace as ds

    monkeypatch.setattr(ds, "free_bytes", lambda _p: 1024)  # 1 KB free
    report = check_writable(tmp_path)
    assert report.ok is False
    assert report.reason == "low-space"
    assert "1.0 KB" in report.error
    assert "free" in report.error


def test_allocation_failure_is_caught_even_when_statvfs_lies(tmp_path, monkeypatch):
    """The real incident: statvfs said 52 GB free, every write failed with ENOSPC."""
    import coworker.diskspace as ds

    monkeypatch.setattr(ds, "free_bytes", lambda _p: 52 * 1024**3)  # "plenty"

    def boom(_dir, _n):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ds, "_probe_allocation", boom)
    report = check_writable(tmp_path)
    assert report.ok is False
    assert report.reason == "alloc-failed"
    assert "cannot allocate" in report.error
    assert "52.0 GB" in report.error  # names what statvfs claimed, for the owner
    assert "No space left" in report.error


def test_unwritable_directory_is_reported(tmp_path, monkeypatch):
    import coworker.diskspace as ds

    def boom(self, parents=False, exist_ok=False):
        raise PermissionError("read-only volume")

    monkeypatch.setattr(ds.Path, "mkdir", boom)
    report = check_writable(tmp_path / "nope")
    assert report.ok is False
    assert report.reason == "unwritable"
    assert "read-only volume" in report.error


def test_free_bytes_and_human_size_are_defensive():
    assert free_bytes(Path(".")) is None or free_bytes(Path(".")) > 0
    assert human_size(None) == "unknown"
    assert human_size(0) == "0 B"
    assert human_size(2 * 1024**3) == "2.0 GB"


# -- artifact verification ----------------------------------------------------------------


def test_artifact_refs_finds_markdown_and_bare_forms():
    text = (
        "**文件：** [战略总纲.md](artifact:战略总纲.md)\n"
        "另见 artifact:notes/plan.md 与重复的 [x](artifact:战略总纲.md)"
    )
    assert artifact_refs(text) == ["战略总纲.md", "notes/plan.md"]


def test_verify_passes_for_a_real_deliverable(tmp_path):
    (tmp_path / "doc.md").write_text("# 交付\n" + "内容\n" * 300, encoding="utf-8")
    assert verify_artifacts("[doc](artifact:doc.md)", tmp_path) == []


def test_verify_flags_the_empty_file_that_a_full_disk_leaves(tmp_path):
    (tmp_path / "空文档.md").write_text("", encoding="utf-8")  # created, never written
    problems = verify_artifacts("[方案](artifact:空文档.md)", tmp_path)
    assert len(problems) == 1
    assert "空文档.md" in problems[0]
    assert "0 字节" in problems[0]


def test_verify_flags_missing_and_truncated_artifacts(tmp_path):
    (tmp_path / "tiny.md").write_text("嗯", encoding="utf-8")
    problems = verify_artifacts(
        "[missing](artifact:没有这个.md) · [tiny](artifact:tiny.md)", tmp_path
    )
    assert any("不存在" in p for p in problems)
    assert any("异常小" in p for p in problems)


def test_verify_is_silent_when_nothing_is_claimed(tmp_path):
    assert verify_artifacts("任务完成，未产出文件。", tmp_path) == []
    assert artifact_warning([]) == ""


def test_artifact_warning_is_reviewer_visible():
    note = artifact_warning(["doc.md: 产物为空文件（0 字节）"])
    assert "产物落地校验未通过" in note
    assert "doc.md" in note


# -- storage failures must not be silent ---------------------------------------------------


class _DeadSink:
    """Stands in for a run store on a full volume."""

    def __init__(self):
        self.calls = 0

    def __call__(self, _kind, _payload):
        self.calls += 1
        raise OSError(13, "database or disk is full")


class _NullProvider(ProviderClient):
    """The storage tests never reach a model — they only exercise the event sink."""

    def complete(self, *, model, messages, tools=None, **settings):  # pragma: no cover
        raise AssertionError("no model call expected")

    def capabilities(self, model):  # pragma: no cover
        return ModelCapabilities()


def _bare_orch(**kwargs):
    from coworker.orchestrator.orchestrator import Orchestrator

    return Orchestrator(provider=_NullProvider(), model="m", workspace=".", **kwargs)


def test_failing_event_sink_reports_once_and_stops_recording():
    sink = _DeadSink()
    seen: list[BaseException] = []
    orch = _bare_orch(event_sink=sink, on_storage_error=seen.append)

    for i in range(10):
        orch._emit("worker_thought", {"i": i})

    # Three attempts trip the limit; the hook fires exactly once; nothing more is tried.
    assert sink.calls == 3
    assert len(seen) == 1
    assert orch.storage_failed is True
    assert "disk is full" in (orch.storage_error or "")


def test_healthy_sink_keeps_recording_and_reports_nothing():
    seen: list[BaseException] = []
    events: list[str] = []
    orch = _bare_orch(
        event_sink=lambda kind, _p: events.append(kind), on_storage_error=seen.append
    )
    for _ in range(5):
        orch._emit("worker_thought", {})
    assert len(events) == 5
    assert seen == []
    assert orch.storage_error is None


def test_transient_sink_failure_does_not_kill_the_run():
    """One blip then recovery must reset the counter (no false alarm)."""
    calls = {"n": 0}

    def flaky(_kind, _payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(13, "database or disk is full")

    seen: list[BaseException] = []
    orch = _bare_orch(event_sink=flaky, on_storage_error=seen.append)
    for _ in range(4):
        orch._emit("worker_thought", {})
    assert orch.storage_failed is False
    assert seen == []
    assert calls["n"] == 4
