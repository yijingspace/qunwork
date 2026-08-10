# -*- coding: utf-8 -*-
"""Token usage ledger + cache-hit-rate accounting (UsageStore)."""

import tempfile
from pathlib import Path

from coworker.usage import UsageStore


def _store():
    return UsageStore(Path(tempfile.mkdtemp()) / "usage.db")


def test_record_and_totals_with_cache_split():
    u = _store()
    u.record({"session_id": "s1", "model": "m", "prompt_tokens": 100, "completion_tokens": 20,
              "cached_tokens": 80, "cache_miss_tokens": 20, "ts": 1780000000.0})
    u.record({"session_id": "s1", "model": "m", "prompt_tokens": 200, "completion_tokens": 30,
              "cached_tokens": 150, "cache_miss_tokens": 50, "ts": 1780001000.0})
    t = u.totals()
    assert t["prompt_tokens"] == 300
    assert t["completion_tokens"] == 50
    assert t["cached_tokens"] == 230
    assert t["turns"] == 2
    assert abs(t["cache_hit_rate"] - 230 / 300) < 5e-4


def test_session_total_and_by_session():
    u = _store()
    u.record({"session_id": "a", "prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 8, "ts": 1780000000.0})
    u.record({"session_id": "b", "prompt_tokens": 50, "completion_tokens": 5, "cached_tokens": 0, "ts": 1780000000.0})
    assert u.session_total("a")["prompt_tokens"] == 10
    rows = u.by_session()
    by = {r["session_id"]: r for r in rows}
    assert by["b"]["prompt_tokens"] == 50
    assert by["a"]["cache_hit_rate"] == 0.8


def test_by_day_grouping():
    u = _store()
    import time
    now = time.time()
    u.record({"session_id": "a", "prompt_tokens": 10, "completion_tokens": 1, "cached_tokens": 5, "ts": now})
    days = u.by_day(days=7)
    assert days, "at least today's bucket"
    assert days[-1]["prompt_tokens"] == 10


def test_no_usage_returns_zeros():
    u = _store()
    t = u.totals()
    assert t["prompt_tokens"] == 0
    assert t["cache_hit_rate"] == 0.0
    assert t["turns"] == 0
