"""HORNET — knowledge-field health assessment (E: 知识场健康度).

Three dimensions, each 0..100, weighted into a single health score:

  1. STRUCTURE (30%) — is the hive well-connected?
       isolation rate (degree-0 cells), cavity rate (never-resonated cells),
       mean degree.
  2. DYNAMICS   (40%) — is the field actively resonating and balanced?
       zone energy balance (Z+/XY/Z- should be ~1/3 each), resonance coverage
       (share of cells that ever resonated), instability (conflict+attractor
       emergents), mean load_factor.
  3. EVOLUTION  (30%) — is the hive growing and staying fresh?
       fission offspring share, mean freshness, hypernode compression activity.

Score → rating: >=80 healthy, 60-79 sub-healthy, <60 warning.
"""
from __future__ import annotations

from typing import Any

from .store import HornetStore

RATING = [(80, "healthy"), (60, "sub-healthy"), (0, "warning")]


def _rating(score: float) -> str:
    for threshold, name in RATING:
        if score >= threshold:
            return name
    return "warning"


def _share(part: float, total: float) -> float:
    return part / total if total > 0 else 0.0


def assess_health(store: HornetStore) -> dict[str, Any]:
    nodes = store.list_nodes()
    edges = store.list_edges()
    node_total = len(nodes)
    n = node_total
    if n == 0:
        return {
            "score": 0, "rating": "empty", "dimensions": {}, "metrics": {},
            "note": "hive empty — run build first",
        }

    # --- metrics ------------------------------------------------------------
    degree: dict[int, int] = {}
    for e in edges:
        degree[e["src"]] = degree.get(e["src"], 0) + 1
        degree[e["dst"]] = degree.get(e["dst"], 0) + 1

    hit_stats = store.node_hit_stats(400)
    hit_ids = set(hit_stats.keys())
    isolated = sum(1 for nid in nodes if degree.get(nid["id"], 0) == 0)
    cavity = sum(1 for nid in nodes if nid["id"] not in hit_ids)

    zone_count = {"z+": 0, "xy": 0, "z-": 0}
    for nd in nodes:
        z = nd.get("z", 0)
        zone_count["z+" if z > 0 else "z-" if z < 0 else "xy"] += 1

    emergents = store.list_emergent(500)
    from collections import Counter

    kind_count = Counter(e["kind"] for e in emergents)
    instability = kind_count.get("conflict", 0) + kind_count.get("attractor", 0)
    fission = kind_count.get("fission", 0)
    hypernodes = kind_count.get("hypernode", 0)

    freshness = [nd.get("freshness", 1.0) for nd in nodes]
    mean_fresh = sum(freshness) / len(freshness)

    resonance_runs = store.recent_resonance(400)
    resonance_coverage = _share(len(hit_ids), node_total)
    mean_load = (sum(s.get("load_factor", 0.0) for s in hit_stats.values()) / node_total) if node_total else 0.0

    # fission offspring = nodes whose title carries a fission marker
    fission_share = _share(
        sum(1 for nd in nodes if "推演延伸" in nd["title"] or "溯源锚点" in nd["title"]), node_total
    )

    # --- dimension scores ---------------------------------------------------
    # STRUCTURE
    isolation_rate = _share(isolated, node_total)
    cavity_rate = _share(cavity, node_total)
    mean_degree = (2 * len(edges)) / n
    struct_score = 100.0 * (
        1.0 - 0.5 * isolation_rate - 0.5 * cavity_rate
    ) * min(1.0, mean_degree / 2.0)

    # DYNAMICS: zone balance = 1 - normalized deviation from 1/3
    zone_balance = 1.0 - sum(abs(_share(v, node_total) - 1 / 3) for v in zone_count.values()) / (4 / 3)
    instab_penalty = min(1.0, instability / 5.0)
    dyn_score = 100.0 * (
        0.4 * max(0.0, zone_balance)
        + 0.3 * resonance_coverage
        + 0.3 * (1.0 - instab_penalty)
    )

    # EVOLUTION
    evo_score = 100.0 * (
        0.4 * min(1.0, fission_share * 4.0)
        + 0.4 * mean_fresh
        + 0.2 * min(1.0, hypernodes / 10.0)
    )

    score = round(
        0.30 * struct_score + 0.40 * dyn_score + 0.30 * evo_score, 1
    )
    return {
        "score": score,
        "rating": _rating(score),
        "dimensions": {
            "structure": round(struct_score, 1),
            "dynamics": round(dyn_score, 1),
            "evolution": round(evo_score, 1),
        },
        "metrics": {
            "nodes": n,
            "edges": len(edges),
            "mean_degree": round(mean_degree, 2),
            "isolation_rate": round(isolation_rate, 4),
            "cavity_rate": round(cavity_rate, 4),
            "zone": {k: round(_share(v, n), 4) for k, v in zone_count.items()},
            "resonance_coverage": round(resonance_coverage, 4),
            "mean_load_factor": round(mean_load, 4),
            "instability": instability,
            "fission": fission,
            "hypernodes": hypernodes,
            "mean_freshness": round(mean_fresh, 4),
            "resonance_runs_7d": len(store.recent_resonance(400)),
        },
    }


def render_hive_health_report(health: dict[str, Any]) -> str:
    """Render the health assessment as a Markdown weekly report (E)."""
    import datetime

    m = health.get("metrics", {})
    d = health.get("dimensions", {})
    zone = m.get("zone", {})
    lines = [
        "# 🐝 知识场健康度周报(HORNET)",
        "",
        f"> 生成时间:{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"## 总评分:**{health.get('score')} / 100** · 评级:`{health.get('rating')}`",
        "",
        "## 三维度",
        f"- **结构** {d.get('structure', 0)}/100:节点 {m.get('nodes', 0)} · 边 {m.get('edges', 0)} · 平均度 {m.get('mean_degree', 0)} · 孤立率 {m.get('isolation_rate', 0)} · 腔体率 {m.get('cavity_rate', 0)}",
        f"- **动态** {d.get('dynamics', 0)}/100:zone 分布 Z+ {zone.get('z+', 0)} / XY {zone.get('xy', 0)} / Z- {zone.get('z-', 0)} · 共振覆盖 {m.get('resonance_coverage', 0)} · 不稳定项 {m.get('instability', 0)}",
        f"- **演进** {d.get('evolution', 0)}/100:平均新鲜度 {m.get('mean_freshness', 0)} · 蜂胞分裂 {m.get('fission', 0)} · 超节点 {m.get('hypernodes', 0)}",
        "",
        "## 关注点",
    ]
    watch: list[str] = []
    if (m.get("cavity_rate") or 0) > 0.3:
        watch.append(f"腔体率 {m.get('cavity_rate')} 偏高——存在大量从未被共振命中的知识盲区")
    if (m.get("isolation_rate") or 0) > 0.3:
        watch.append(f"孤立节点率 {m.get('isolation_rate')} 偏高——知识间缺乏语义连接")
    if (m.get("mean_freshness") or 0) < 0.4:
        watch.append(f"平均新鲜度 {m.get('mean_freshness')} 偏低——知识老化,建议高频共振激活")
    if (m.get("instability") or 0) >= 3:
        watch.append(f"冲突/奇异吸引子 {m.get('instability')} 项——存在知识矛盾,建议人工裁决")
    if (m.get("zone") or {}).get("z-", 0) > 0.6:
        watch.append("Z- 溯源层占比过高——组织偏向回顾,推演层(Z+)不足")
    if not watch:
        watch.append("各项指标正常,蜂巢处于健康状态 ✓")
    lines += [f"- {w}" for w in watch]
    lines += ["", "## 建议动作", "- 对腔体率高的区域执行「自我提问式探索」补全(F 特性)"]
    lines += ["- 对孤立节点建议补充语义关联或合并"]
    lines += ["- 周期性共振高频主题,保持知识新鲜度"]
    return "\n".join(lines)
