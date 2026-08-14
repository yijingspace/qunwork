"""Tests for 建议10 — AI 团队账本 × 价值归因 (ROI 仪表盘).

Covers: 价值标签 API, 费率配置, 月度 ROI 计算(成本/缓存节省/人工节省/
技能复用), HTML 报告生成。全部本地计算, 无外部依赖。
"""

from __future__ import annotations

import pytest

from coworker.roi import compute_monthly, compute_skill_reuse, render_html
from coworker.usage import UsageStore


def _month_bounds(month: str) -> tuple[float, float]:
    import time
    from datetime import date

    y, mo = (int(x) for x in month.split("-"))
    start = time.mktime(date(y, mo, 1).timetuple())
    end = time.mktime(date(y + (mo == 12), mo % 12 + 1, 1).timetuple())
    return start, end


def _usage_store(tmp_path, month: str, model="deepseek-chat") -> UsageStore:
    u = UsageStore(tmp_path / "usage.db")
    start, end = _month_bounds(month)
    u.record({"session_id": "s1", "surface": "t", "model": model,
              "prompt_tokens": 1_000_000, "completion_tokens": 100_000,
              "cached_tokens": 400_000, "ts": start + 1})
    # 月外数据不计入
    u.record({"session_id": "s2", "surface": "t", "model": model,
              "prompt_tokens": 9_000_000, "completion_tokens": 0,
              "cached_tokens": 0, "ts": end + 100})
    return u


class _Runs:
    """run_store 轻量 stub(list_runs 返回可控数据)."""

    def __init__(self, runs):
        self._runs = runs

    def list_runs(self, limit=20):
        return self._runs


def _mk_run(run_id, created, updated, tag=None):
    return {"run_id": run_id, "intent": "g", "status": "completed",
            "created_at": created, "updated_at": updated, "value_tag": tag}


def test_compute_monthly_cost_and_cache_saved(tmp_path):
    u = _usage_store(tmp_path, "2026-08")
    runs = _Runs([])
    out = compute_monthly(
        u, runs, month="2026-08",
        rates={"deepseek-chat": {"prompt_ppm": 2.0, "completion_ppm": 8.0}},
        hourly_rate=100.0,
    )
    # prompt 1M × ¥2/1M = ¥2; completion 100k × ¥8/1M = ¥0.8
    assert out["total_cost"] == pytest.approx(2.8)
    # cached 400k × ¥2/1M = ¥0.8
    assert out["cache_saved"] == pytest.approx(0.8)
    assert out["month"] == "2026-08"


def test_compute_monthly_labor_and_tags(tmp_path):
    start, end = _month_bounds("2026-08")
    u = UsageStore(tmp_path / "u.db")
    runs = _Runs(
        [
            _mk_run("r1", start, start + 7200, tag="Q3 客户报告"),   # 2h
            _mk_run("r2", start, start + 3600, tag="Q3 客户报告"),  # 1h
            _mk_run("r3", start, start + 1800, tag="发布 v1.2"),  # 0.5h
            _mk_run("r4", end + 5, end + 100, tag="out-of-month"),
        ]
    )
    out = compute_monthly(
        u, runs, month="2026-08", rates={}, hourly_rate=200.0,
        tag_of_run={"r1": "Q3 客户报告", "r2": "Q3 客户报告", "r3": "发布 v1.2", "r4": "out-of-month"},
    )
    assert out["labor_hours"] == pytest.approx(3.5)  # 2+1+0.5 (r4 不在月内)
    assert out["labor_saved"] == pytest.approx(700.0)
    tags = out["by_tag"]
    assert tags["Q3 客户报告"]["hours"] == pytest.approx(3.0)
    assert tags["发布 v1.2"]["runs"] == 1
    # 月外 run 不计入标签
    assert "out-of-month" not in tags


def test_compute_skill_reuse(tmp_path):
    start, end = _month_bounds("2026-08")
    runs = _Runs(
        [
            _mk_run("r1", start, start + 3600),  # 首次 1h
            _mk_run("r2", start, start + 1800),  # 后续 0.5h
            _mk_run("r3", start, start + 1200),  # 后续 0.33h
        ]
    )
    skill = compute_skill_reuse(
        UsageStore(tmp_path / "u.db"), runs, month="2026-08",
        rates={},
        template_of_run={"r1": "tpl-a", "r2": "tpl-a", "r3": "tpl-a"},
    )
    assert len(skill["per_template"]) == 1
    # 首次 3600s − 后续平均 1500s = 2100s × 2 次 = 4200s
    assert skill["total_saved_seconds"] == pytest.approx(4200)


def test_render_html_escapes_and_kpis():
    report = {
        "month": "2026-08", "total_cost": 2.8, "cache_saved": 0.8,
        "labor_hours": 3.5, "labor_saved": 700.0,
        "per_model": [{"model": "deepseek", "prompt_tokens": 1000,
                       "completion_tokens": 100, "cached_tokens": 400,
                       "cost": 0.01, "cache_saved": 0.001}],
        "by_tag": {"<script>": {"runs": 1, "hours": 1.0, "cost": 100.0}},
        "missing_rates": [], "rates_configured": True, "hourly_rate": 200.0,
    }
    html = render_html(report, {"total_saved_seconds": 4200, "per_template": []})
    assert "QunWork AI 团队价值报告" in html
    assert "<script>" not in html  # 标签被转义
    assert "总投入成本" in html and "¥2.80" in html


# -- API 层 -------------------------------------------------------------------
def test_roi_api_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    mgr = SessionManager(data_dir=tmp_path / "data")
    client = TestClient(create_app(mgr))

    # 配置费率
    r = client.post("/v1/roi/config", json={
        "rates": {"deepseek-chat": {"prompt_ppm": 2.0, "completion_ppm": 8.0}},
        "hourly_rate": 150.0,
    })
    assert r.json()["ok"] is True
    assert client.get("/v1/roi/config").json()["hourly_rate"] == 150.0

    # 建一个 run + 打价值标签
    run_id = mgr.orchestration_store.create_run("季度报告")
    r = client.post(f"/v1/orchestrate/{run_id}/tag", json={"value_tag": "Q3 客户报告"})
    assert r.json()["ok"] is True
    assert mgr.orchestration_store.get_run(run_id)["value_tag"] == "Q3 客户报告"

    # 月度报告(空 usage 也返回结构)
    r = client.get("/v1/roi/report?month=2026-08")
    d = r.json()
    assert "total_cost" in d and "cache_saved" in d and "labor_hours" in d
    assert "skill_reuse" in d

    # HTML 报告落盘
    r = client.post("/v1/roi/report/html", json={"month": "2026-08"})
    assert r.json()["ok"] is True
    assert r.json()["path"].endswith("roi-report-2026-08.html")


# -- security-review 修复回归 (MEDIUM 费率校验 / LOW month 格式) ---------------

def test_roi_config_rejects_bad_input(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    mgr = SessionManager(data_dir=tmp_path / "data")
    client = TestClient(create_app(mgr))

    # NaN / 负数 / 非数值 hourly → 拒绝
    r = client.post("/v1/roi/config", json={"rates": {}, "hourly_rate": "abc"})
    assert r.json()["ok"] is False
    r = client.post("/v1/roi/config", json={"rates": {}, "hourly_rate": -5})
    assert r.json()["ok"] is False
    # rates 非对象 → 拒绝
    r = client.post("/v1/roi/config", json={"rates": [1, 2], "hourly_rate": 100})
    assert r.json()["ok"] is False
    # 合法值仍可保存
    r = client.post("/v1/roi/config", json={"rates": {"m": {"prompt_ppm": 2}}, "hourly_rate": 100})
    assert r.json()["ok"] is True


def test_roi_report_rejects_bad_month(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    mgr = SessionManager(data_dir=tmp_path / "data")
    client = TestClient(create_app(mgr))
    assert client.get("/v1/roi/report?month=2026-13").json()["ok"] is False
    assert client.get("/v1/roi/report?month=abc").json()["ok"] is False
    assert client.get("/v1/roi/report?month=2026-08").status_code == 200
