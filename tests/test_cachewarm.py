"""Tests for P0 建议1 — HORNET coldness × cache warm-up.

The provider is faked (no network): the warm pass must select the coldest
knowledge nodes, fire max_tokens=1 probes, and record every probe in the usage
store under surface='cachewarm' so the UI can show warm-up spend separately.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from coworker.cachewarm import CacheWarmer
from coworker.usage import UsageStore


class _FakeChunk:
    def __init__(self, usage=None):
        self.turn = SimpleNamespace(usage=usage) if usage else None


class _FakeProvider:
    """Records calls; yields one chunk with a configurable usage trailer."""

    def __init__(self, usage=None):
        self.usage = usage
        self.calls: list[dict] = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        yield _FakeChunk(self.usage)


def _fake_hornet(nodes):
    class H:
        def list_nodes(self):
            return nodes

    return H()


@pytest.fixture
def warmer(tmp_path):
    usage = UsageStore(tmp_path / "usage.db")
    nodes = [
        {"title": "hot", "content": "very fresh", "freshness": 0.95},
        {"title": "cold", "content": "stale doc", "freshness": 0.12},
        {"title": "coldest", "content": "colder doc", "freshness": 0.03},
    ]
    provider = _FakeProvider(
        {
            "prompt_tokens": 120,
            "completion_tokens": 1,
            "cached_tokens": 0,
            "cache_miss_tokens": 120,
        }
    )
    w = CacheWarmer(
        provider, _fake_hornet(nodes), usage, default_model="deepseek-chat"
    )
    return w, provider, usage


def test_cold_nodes_selects_lowest_freshness_first(warmer):
    w, _, _ = warmer
    cold = w.cold_nodes(2)
    assert [n["title"] for n in cold] == ["coldest", "cold"]


async def test_warm_once_probes_coldest_and_records_usage(warmer):
    w, provider, usage = warmer
    out = await w.warm_once()
    assert out["ok"] is True
    assert out["warmed"] == 3  # all three nodes probed
    assert out["prompt_tokens"] == 360  # 3 × 120
    # probes rode max_tokens=1 and the full content as the user message
    for call in provider.calls:
        assert call["max_tokens"] == 1
        assert call["model"] == "deepseek-chat"
        assert call["messages"][1]["content"]  # knowledge content injected
    # recorded under the cachewarm surface, not user totals
    week = usage.surface_totals("cachewarm", 0)
    assert week["prompt_tokens"] == 360
    assert week["calls"] == 3
    assert usage.totals()["turns"] == 3
    # status reports the week's warm activity
    st = w.status()
    assert st["enabled"] is True
    assert st["week"]["prompt_tokens"] == 360


async def test_warm_once_no_model_or_no_nodes_is_safe(warmer, tmp_path):
    w, _, usage = warmer
    w.default_model = None
    out = await w.warm_once()
    assert out["ok"] is False and out["warmed"] == 0

    w2 = CacheWarmer(
        _FakeProvider(), _fake_hornet([]), UsageStore(tmp_path / "u2.db"),
        default_model="deepseek-chat",
    )
    out2 = await w2.warm_once()
    assert out2["ok"] is False and "no knowledge nodes" in out2["reason"]


async def test_warm_once_provider_error_does_not_block_loop(warmer):
    w, provider, usage = warmer
    async def broken(**kwargs):
        yield _FakeChunk()
        raise RuntimeError("provider down")

    provider.stream = broken
    out = await w.warm_once()
    assert out["ok"] is True
    assert out["warmed"] == 0
    assert out["errors"] == 3
    # no usage rows for failed probes
    assert usage.surface_totals("cachewarm", 0)["calls"] == 0


def test_surface_totals_filters_by_surface(tmp_path):
    usage = UsageStore(tmp_path / "u.db")
    usage.record({"session_id": "s1", "surface": "cowork", "prompt_tokens": 500})
    usage.record({"session_id": "__cachewarm__", "surface": "cachewarm", "prompt_tokens": 90})
    assert usage.surface_totals("cachewarm", 0)["prompt_tokens"] == 90
    assert usage.surface_totals("cowork", 0)["prompt_tokens"] == 500


async def test_warm_once_serialized_by_lock(warmer):
    """Two concurrent warm passes must not interleave provider probes."""
    w, provider, _ = warmer
    # make the provider slow so the lock matters
    async def slow(**kwargs):
        provider.calls.append(kwargs)
        await asyncio.sleep(0.01)
        yield _FakeChunk(
            {"prompt_tokens": 10, "completion_tokens": 1,
             "cached_tokens": 0, "cache_miss_tokens": 10}
        )

    provider.stream = slow
    await asyncio.gather(w.warm_once(), w.warm_once())
    assert len(provider.calls) == 6  # 3 nodes × 2 passes, not interleaved


def test_manager_cache_warm_toggle_and_status(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from coworker.server.manager import SessionManager

    manager = SessionManager(data_dir=tmp_path / "data")
    assert manager.cache_warm_status()["enabled"] is True
    assert manager.cache_warm_toggle(False)["enabled"] is False
    assert manager.cache_warm_status()["enabled"] is False
    assert manager.cache_warm_toggle(True)["enabled"] is True
