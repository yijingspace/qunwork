"""AI 团队账本 × 价值归因 (建议10: 给老板看的 ROI 仪表盘).

计算口径(全部本地、透明可复核):
- 总投入成本 = Σ(prompt_tokens×prompt_ppm + completion_tokens×completion_ppm) / 1M
  费率按 model 配置(本地填自己的 API 计费单价, 未配置的 model 按 0 计并标注)。
- 缓存节省金额 = Σ cached_tokens × prompt_ppm / 1M
  (命中部分按全部按 prompt 单价折算 — 保守估计, 因为命中省的就是输入 token 费)。
- 节省人工小时 = Σ run 时长(updated_at − created_at 秒) / 3600 × hourly_rate。
- 技能复用节省 = 同 template_id 的 runs: (首次成本 − 后续平均成本) × 后续次数
  (模板化后第 N 次比首次便宜的部分累计)。
- 项目价值标签: run 可打业务标签(如「Q3 客户报告」), 报告按标签分组。

报告输出: Markdown + HTML(浏览器可直接打印为 PDF), 不引入 PDF 新依赖。
"""

from __future__ import annotations

import html
from typing import Any, Optional


def cost_for(
    tokens: int,
    ppm: Optional[float],
) -> float:
    """Tokens → cost (per-million price). 未配置费率按 0(标注 missing)。"""
    if ppm is None or ppm <= 0:
        return 0.0
    return tokens * ppm / 1_000_000


def compute_monthly(
    usage_store: Any,
    run_store: Any,
    *,
    month: str,  # "YYYY-MM"
    rates: dict[str, dict[str, float]],  # {model: {prompt_ppm, completion_ppm}}
    hourly_rate: float,
    tag_of_run: Optional[dict[str, str]] = None,  # run_id -> value_tag
) -> dict:
    """One month's ROI ledger. month filter is applied on token_usage.created_at
    (YYYY-MM prefix of local date) and orchestration_runs.created_at."""
    import time
    from datetime import date, timedelta

    def _month_range(m: str) -> tuple[float, float]:
        y, mo = (int(x) for x in m.split("-"))
        start = time.mktime(date(y, mo, 1).timetuple())
        end = time.mktime(date(y + (mo == 12), mo % 12 + 1, 1).timetuple())
        return start, end

    start_ts, end_ts = _month_range(month)

    # -- token 成本 (usage_store) --
    # 从 token_usage 聚合本月: 按 model 分组 prompt/completion/cached。
    model_rows: dict[str, dict[str, int]] = {}
    with usage_store._lock:
        rows = usage_store._con.execute(
            "SELECT model, COALESCE(SUM(prompt_tokens),0) p, "
            "COALESCE(SUM(completion_tokens),0) c, COALESCE(SUM(cached_tokens),0) h "
            "FROM token_usage WHERE created_at >= ? AND created_at < ? GROUP BY model",
            (start_ts, end_ts),
        ).fetchall()
    for r in rows:
        model_rows[r["model"] or "unknown"] = {
            "prompt": int(r["p"]), "completion": int(r["c"]), "cached": int(r["h"]),
        }

    total_cost = 0.0
    cache_saved = 0.0
    missing_rates: list[str] = []
    per_model: list[dict[str, Any]] = []
    for model, t in sorted(model_rows.items()):
        rate = rates.get(model) or {}
        prompt_ppm = rate.get("prompt_ppm")
        completion_ppm = rate.get("completion_ppm")
        if prompt_ppm is None or completion_ppm is None:
            missing_rates.append(model)
        cost = cost_for(t["prompt"], prompt_ppm) + cost_for(t["completion"], completion_ppm)
        saved = cost_for(t["cached"], prompt_ppm)
        total_cost += cost
        cache_saved += saved
        per_model.append(
            {
                "model": model,
                "prompt_tokens": t["prompt"],
                "completion_tokens": t["completion"],
                "cached_tokens": t["cached"],
                "cost": round(cost, 4),
                "cache_saved": round(saved, 4),
            }
        )

    # -- 人工节省 (run_store 时长) --
    runs = run_store.list_runs(limit=10_000)
    labor_hours = 0.0
    tag_totals: dict[str, dict[str, Any]] = {}
    for r in runs:
        ts = r.get("created_at") or 0
        if not (start_ts <= ts < end_ts):
            continue
        dur = max(0.0, (r.get("updated_at") or ts) - ts) / 3600.0
        labor_hours += dur
        tag = (tag_of_run or {}).get(r["run_id"]) or "未标签"
        bucket = tag_totals.setdefault(
            tag, {"runs": 0, "hours": 0.0, "cost": 0.0}
        )
        bucket["runs"] += 1
        bucket["hours"] += dur
        bucket["cost"] += dur * hourly_rate
    labor_cost = labor_hours * hourly_rate

    return {
        "month": month,
        "total_cost": round(total_cost, 4),
        "cache_saved": round(cache_saved, 4),
        "labor_hours": round(labor_hours, 3),
        "labor_saved": round(labor_cost, 4),
        "per_model": per_model,
        "by_tag": tag_totals,
        "missing_rates": missing_rates,
        "rates_configured": bool(rates),
        "hourly_rate": hourly_rate,
    }


def compute_skill_reuse(
    usage_store: Any,
    run_store: Any,
    *,
    month: str,
    rates: dict[str, dict[str, float]],
    template_of_run: Optional[dict[str, Optional[str]]] = None,
) -> dict:
    """技能复用节省: 同 template_id 首次运行成本 vs 后续平均成本之差 × 后续次数。
    需要 run 的成本 → 通过 run 事件里的 model/token 关联(简化: 按 template 内
    run 数量与总成本占比估算, 见下方实现)。"""
    import time
    from datetime import date

    y, mo = (int(x) for x in month.split("-"))
    start_ts = time.mktime(date(y, mo, 1).timetuple())
    end_ts = time.mktime(date(y + (mo == 12), mo % 12 + 1, 1).timetuple())

    runs = run_store.list_runs(limit=10_000)
    by_template: dict[str, list[dict]] = {}
    for r in runs:
        ts = r.get("created_at") or 0
        if not (start_ts <= ts < end_ts):
            continue
        tid = (template_of_run or {}).get(r["run_id"])
        if tid:
            by_template.setdefault(tid, []).append(r)

    total_saved = 0.0
    per_template: list[dict[str, Any]] = []
    for tid, rs in sorted(by_template.items()):
        if len(rs) < 2:
            continue
        # 成本代理: run 时长(分钟) × hourly 等价 — 首次 vs 后续的差值。
        first_dur = max(0.0, (rs[0].get("updated_at") or 0) - rs[0].get("created_at", 0))
        later = rs[1:]
        later_dur_avg = sum(
            max(0.0, (r.get("updated_at") or 0) - r.get("created_at", 0)) for r in later
        ) / len(later)
        diff = max(0.0, first_dur - later_dur_avg)
        saved = diff * len(later)  # 秒差累计 → 后续报告换算
        per_template.append(
            {"template_id": tid, "runs": len(rs), "reuse_count": len(later),
             "saved_seconds": round(saved, 1)}
        )
        total_saved += saved
    return {"total_saved_seconds": round(total_saved, 1), "per_template": per_template}


def render_html(report: dict, skill: dict) -> str:
    """HTML 报告(浏览器可直接打印为 PDF)。"""
    rows = "\n".join(
        f"<tr><td>{html.escape(m['model'])}</td><td>{m['prompt_tokens']:,}</td>"
        f"<td>{m['completion_tokens']:,}</td><td>{m['cached_tokens']:,}</td>"
        f"<td>¥{m['cost']:.2f}</td><td>¥{m['cache_saved']:.2f}</td></tr>"
        for m in report["per_model"]
    )
    tags = "\n".join(
        f"<tr><td>{html.escape(t)}</td><td>{b['runs']}</td><td>{b['hours']:.1f}h</td>"
        f"<td>¥{b['cost']:.2f}</td></tr>"
        for t, b in sorted(report["by_tag"].items())
    )
    missing = (
        f"<p class='warn'>⚠️ 以下模型未配置费率(按 0 计): {html.escape(', '.join(report['missing_rates']))}</p>"
        if report["missing_rates"]
        else ""
    )
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>QunWork AI 团队价值报告 {report['month']}</title>
<style>
 body {{ font: 14px/1.6 system-ui, sans-serif; margin: 24px auto; max-width: 760px; color: #222; }}
 h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; margin-top: 22px; }}
 table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
 th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; }}
 th {{ background: #f5f5f5; }}
 .kpi {{ display: flex; gap: 14px; flex-wrap: wrap; margin: 14px 0; }}
 .kpi div {{ border: 1px solid #e0e0e0; border-radius: 10px; padding: 12px 16px; min-width: 150px; }}
 .kpi b {{ font-size: 20px; display: block; }}
 .warn {{ color: #b45309; }}
</style></head><body>
<h1>🐝 QunWork AI 团队价值报告 · {html.escape(report['month'])}</h1>
<div class="kpi">
  <div>总投入成本<b>¥{report['total_cost']:.2f}</b></div>
  <div>缓存节省<b>¥{report['cache_saved']:.2f}</b></div>
  <div>节省人工<b>{report['labor_hours']:.1f}h</b><b style="font-size:14px">≈ ¥{report['labor_saved']:.2f}</b></div>
</div>
{missing}
<h2>按模型 Token 成本</h2>
<table><tr><th>模型</th><th>输入</th><th>输出</th><th>缓存</th><th>成本</th><th>缓存节省</th></tr>{rows}</table>
<h2>按项目价值标签</h2>
<table><tr><th>标签</th><th>运行数</th><th>时长</th><th>人工价值</th></tr>{tags}</table>
<h2>技能复用节省</h2>
<p>模板化复用累计节省 <b>{skill['total_saved_seconds'] / 60:.1f} 分钟</b>
({len(skill['per_template'])} 个模板) — 首次 vs 第 N 次运行时长差。</p>
<p class="warn">费率与本报告均在本地计算, 不发送任何数据。PDF 请在浏览器中打印本页。</p>
</body></html>"""
