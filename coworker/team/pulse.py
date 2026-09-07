"""组织脉搏 (方案E) — 组织首页的实时运行聚合视图。

数据源全部是**既有留痕**, 零新增写路径:

- audit_log      → 治理时间线 (矩阵变更/成员生命周期/门禁拦截) + 资金拦截流
- sync_changes   → 组织网络同步活动 (P2P 收到/产生过什么变更)
- task_groups + members → 责任链 (哪个目标归谁负责、谁在执行、进展多久)
"""

from __future__ import annotations

import time
from typing import Any, Optional

_STATE_ORDER = {"active": 0, "reviewing": 1, "forming": 2, "dissolved": 3}


def _hhmm(delta_seconds: float) -> str:
    h = delta_seconds / 3600.0
    if h < 1:
        return f"{int(max(0, delta_seconds) // 60)}m"
    if h < 48:
        return f"{round(h)}h"
    return f"{round(h / 24)}d"


def collect_pulse(
    store: Any,
    *,
    window_days: float = 30.0,
    timeline_limit: int = 30,
    now: Optional[float] = None,
) -> dict:
    """聚合一份组织脉搏。纯读, 传入 TeamStore 即可 (manager 层只做转发)。"""
    now = now if now is not None else time.time()
    cutoff = now - window_days * 86400

    # -- 时间线: 治理事件 + 同步活动, 按时间倒序合并 ----------------------------
    timeline: list[dict] = []
    for e in store.query_audit(limit=timeline_limit):
        timeline.append({
            "ts": e["ts"], "kind": "governance", "action": e["action"],
            "actor": e["actor"], "target": e["target"], "detail": e["detail"],
        })
    for c in store.recent_sync_changes(limit=15):
        timeline.append({
            "ts": c["ts"], "kind": "sync",
            "action": f"sync.{c['entity_type']}.{c['op']}",
            "actor": c["author"], "target": c["entity_id"], "detail": {},
        })
    timeline.sort(key=lambda x: -float(x["ts"] or 0))
    timeline = timeline[:timeline_limit]

    # -- 资金流: 窗口内被组织门禁拦截的资金动作 ---------------------------------
    blocked_total = 0.0
    blocked_items: list[dict] = []
    by_role: dict[str, float] = {}
    for e in store.query_audit(limit=200, action="org_gate.fund_deny"):
        if float(e["ts"]) < cutoff:
            continue  # query_audit 按 seq 倒序, ts 不保证单调 — 跳过而非中断
        amount = float((e.get("detail") or {}).get("amount") or 0)
        blocked_total += amount
        by_role[e["target"]] = by_role.get(e["target"], 0.0) + amount
        blocked_items.append({
            "ts": e["ts"], "amount": amount, "role": e["target"],
            "tool": (e.get("detail") or {}).get("tool"),
        })
    gate_events = store.query_audit(limit=200, action="org_gate.deny")
    gate_window = [e for e in gate_events if float(e["ts"]) >= cutoff]

    # -- 责任链: 活跃/评审/组建中的蜂群, 目标 → 负责人 → 成员/agent ---------------
    members_by_id = {m["id"]: m for m in store.list_members()}
    chains: list[dict] = []
    for g in store.list_task_groups(include_dissolved=True):
        if g["state"] == "dissolved" and float(g.get("dissolved_at") or 0) < now - 7 * 86400:
            continue  # 解散超一周的退场, 近期归档仍可瞥见
        owner = members_by_id.get(str(g.get("owner_member") or ""))
        chains.append({
            "id": g["id"],
            "goal": (g.get("goal") or "")[:80],
            "state": g["state"],
            "owner": (
                {"id": owner["id"], "name": owner["name"], "role": owner["role"]}
                if owner else None
            ),
            "members": [
                {
                    "id": mid,
                    "name": (members_by_id.get(mid) or {}).get("name", mid),
                    "role": (members_by_id.get(mid) or {}).get("role", "?"),
                    "status": (members_by_id.get(mid) or {}).get("status", "offline"),
                }
                for mid in (g.get("member_ids") or [])[:6]
            ],
            "agent_count": len(g.get("agent_ids") or []),
            "age": _hhmm(now - float(g.get("created_at") or now)),
        })
    chains.sort(key=lambda c: (_STATE_ORDER.get(c["state"], 9), -float(0)))
    # 状态序内保持 created_at DESC (list_task_groups 本已倒序, sort 稳定)
    chains = chains[:8]

    # -- 名册概览 ---------------------------------------------------------------
    ms = store.list_members()
    roster = {
        "total": len(ms),
        "online": sum(1 for m in ms if m.get("status") == "online"),
        "invited": sum(1 for m in ms if m.get("status") == "invited"),
        "offline": sum(1 for m in ms if m.get("status") not in ("online", "invited")),
    }

    # -- 治理摘要 ---------------------------------------------------------------
    matrix_events = [
        e for e in store.query_audit(limit=200, action="matrix.%")
        if float(e["ts"]) >= cutoff
    ]

    return {
        "generated_at": now,
        "window_days": window_days,
        "timeline": timeline,
        "fund_flow": {
            "window_days": window_days,
            "blocked_total": round(blocked_total, 2),
            "blocked_count": len(blocked_items),
            "by_role": {k: round(v, 2) for k, v in sorted(by_role.items(), key=lambda x: -x[1])},
            "recent": blocked_items[:10],
            "capability_denies": len(gate_window),
        },
        "responsibility": chains,
        "roster": roster,
        "governance": {"matrix_changes": len(matrix_events)},
    }
